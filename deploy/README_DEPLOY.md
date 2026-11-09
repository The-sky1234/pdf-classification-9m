# 阶段一服务器部署（单机 1×RTX A6000 48GB）

所有命令都在仓库的 `deploy/` 目录下执行，配置只有一份 `deploy/.env`。

## 架构

| 服务 | 镜像 | 做什么 | 何时运行 |
|---|---|---|---|
| `postgres` | postgres:16 | 任务台账 `phase1_files`（每份主件一行） | 常驻 |
| `api` | `Dockerfile.api` | 健康检查、`/metrics`、台账查询、复核改判 | 常驻 |
| `cpu-worker` | `Dockerfile.api` | 波次 0（抽文字 + 规则）和归档复制 | 常驻 |
| `ocr-worker` | `Dockerfile.ocr`（CUDA 11.8 + Paddle 2.6.2） | 波次 1：PaddleOCR 识别扫描页和图片 | `--profile ocr` |
| `classify-worker` | `Dockerfile.infer`（torch 2.4.1 cu121） | 波次 2：bge-m3 + bge-reranker-v2-m3 选场景 | `--profile classify` |
| `prometheus`、`dcgm-exporter` | 官方镜像 | 监控 | `--profile monitoring` |

文件的流转顺序是：`cpu`，然后 `ocr`（只有扫描件走这一步），然后 `classify`，然后 `archive`，最后 `done`。其中 `failed` 状态需要人工处理。

- 台账用租约领取（`FOR UPDATE SKIP LOCKED`）。进程崩溃、容器重启都不丢任务，租约到期后别的进程会接着做。
- 两个 GPU 波次同一时间只开一个。`ops.sh wave` 和 `autopilot` 负责切换。就算误把两个都开了，它们也会在 `DATA_DIR/locks/` 下的显卡锁 上排队，不会同时占用显卡。
- 源目录只读挂载，归档靠复制再核对 SHA-256，源文件一个字节都不会改。
- 阈值文件没签发之前（`THRESHOLDS_FILE` 为空），分类结果一律进 `04_待复核暂存`，不会自动进业务目录。

## 1. 服务器准备

```bash
# NVIDIA 驱动 >= 525、Docker 24+（带 compose v2 插件）、nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker
git clone <仓库地址> pdfcls && cd pdfcls/deploy
cp .env.example .env && vi .env      # 按注释逐项填写，密码只用字母和数字
sudo ./ops.sh init                   # 建目录、改属主、.env 改成 600
./ops.sh check                       # [FAIL] 为 0 再继续
```

磁盘规划如下：
- `PG_DATA_DIR` 放 SSD，900 万行约 30–60GB。
- `ARCHIVE_ROOT` 要装得下全部主件，而且不能在 `SOURCE_ROOT` 里面。
- `MODEL_DIR` 约 5GB。
- 镜像本身约 21GB：ocr 11GB，infer 9GB，api 0.4GB。

## 2. 构建镜像、下载模型

```bash
./ops.sh build          # 三个镜像都构建；国内把 .env 里的 PIP_INDEX_URL 换成清华源
./ops.sh fetch-models   # PaddleOCR 约 20MB，bge-m3 + reranker 约 4.5GB；国内设 HF_ENDPOINT=https://hf-mirror.com
```

模型下载到 `MODEL_DIR` 以后，GPU 容器就离线运行了（`HF_HUB_OFFLINE=1`）。服务器可以只在这一步联网。

## 3. 启动、灌清单

```bash
./ops.sh up
./ops.sh load /path/to/masters.csv --dry-run          # 只检查不入库
./ops.sh load /path/to/masters.csv                    # 可重复执行，已入库的路径不会重复
```

清单就是已经去重好的文件列表，部署流程不再做任何去重，清单里有多少个路径就处理多少个文件（同一路径出现两次只记一行）。支持两种格式：

- 一行一个完整路径的文本文件，UTF-8 或 GBK 都行；
- CSV，路径列叫 `完整路径`、`路径`、`path`、`full_path` 或 `source_path` 之一。可选列：`文件大小_字节`、`来源`；如果有 `是否主件` 列，只取值为“是”的行。

入库前会做两项检查，不通过就不入库：
- 每条路径都必须在 `SOURCE_ROOT` 下面。
- 抽查前 200 条路径，至少要有一条能打开。

如果清单是在别的机器或别的挂载点上生成的，用 `--map` 换前缀，可以给多次：

```bash
./ops.sh load masters.csv --map '/home/wangzengzheng/data=/mnt/nas/source' --dry-run
./ops.sh load masters.csv --map 'Z:\共享=/mnt/nas/source'      # 反斜杠会一并换成 /
```

`cpu-worker` 起来后自动开始波次 0，并处理归档。

## 4. GPU 波次

推荐在 tmux 里开自动驾驶。它会在当前波次的队列清空后才切换到另一个波次，波次 0 还在出活时会继续等，全部做完后退出：

```bash
tmux new -s autopilot './ops.sh autopilot'
```

也可以手动切换：

```bash
./ops.sh wave ocr        # 停 classify，开 ocr
./ops.sh wave classify   # 停 ocr，开 classify
./ops.sh wave off
```

显存分配如下：
- 每个 OCR 进程约占 2–4GB，`OCR_PROCS=3` 比较稳。
- 分类进程以 fp16 加载两个模型，约占 3GB。`CLASSIFY_PROCS` 一般设 1，GPU 利用率不满时可以加到 2。

## 5. 日常运维

```bash
./ops.sh status            # 各阶段数量、各结论数量、在租约中的行数
./ops.sh logs cpu-worker   # 或 ocr-worker / classify-worker / api
./ops.sh export            # 全部结论导出为 jsonl，放在 DATA_DIR/exports
./ops.sh requeue           # failed 的行回到失败前的阶段重跑（先看 last_error 修好原因）
./ops.sh backup            # pg_dump 到 DATA_DIR/backups，建议每天一次
./ops.sh stop              # 停所有容器，数据都在宿主机目录里
```

复核接口（请求头带 `X-API-Key`）：
- `GET /phase1/files?archive_type=PENDING`：待复核列表。
- `POST /phase1/files/{id}/review`：改判。改判后归档进程会把副本搬到新目录，并删掉旧副本。

`GET /metrics` 不用鉴权，给 Prometheus 抓取。

### 阈值签发

先用首批标注评审定下阈值，把 JSON 文件放到 `DATA_DIR/config/thresholds.json`，内容里要写 `"calibrated": true`。然后在 `.env` 里设置 `THRESHOLDS_FILE=/config/thresholds.json`，再执行 `./ops.sh wave classify` 重启分类容器。字段说明见 `classifier/config.py`。

### 大模型兜底（可选）

`LLM_ENDPOINT` 填任意 OpenAI 兼容接口，比如 vLLM。宿主机上的服务写 `http://host.docker.internal:端口/v1`。`LLM_MAX_CALLS` 是调用次数上限，超过以后一律转人工复核。

## 6. 故障处理

| 现象 | 处理 |
|---|---|
| `load` 报路径不在 `SOURCE_ROOT` 下 | 用 `--map` 换前缀，或者调整 `SOURCE_ROOT`，让它覆盖全部路径 |
| worker 日志一直报“源目录不可读” | NAS 掉线或没挂载。worker 会原地等待，不会把文件打成失败；挂好后自动继续 |
| `failed` 越来越多 | 用 `GET /phase1/files?stage=failed` 看 `last_error`，修好原因后执行 `./ops.sh requeue` |
| OCR 容器报 CUDA 错误 | `./ops.sh check`；`docker run --rm --gpus device=0 nvidia/cuda:11.8.0-base-ubuntu22.04 nvidia-smi` |
| 分类容器报找不到模型 | 没执行 `fetch-models`，或者 `MODEL_DIR/hf` 的属主不是 `APP_UID` |
| 进程被杀、机器重启 | 不用处理。租约到期后会自动重领，归档复制也是幂等的 |

## 7. 本地开发和测试

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r deploy/requirements-dev.txt
python -m pytest tests -q
```

单元测试用 SQLite 和替身模型，不需要 GPU，也不需要下载模型。
