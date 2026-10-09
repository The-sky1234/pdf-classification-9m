# SERVER_DEPLOY_GUIDE.md — 单台Linux GPU服务器部署指南（与代码一致版）

> 状态：本指南根据 `deploy/` 实际文件生成，未连接真实服务器，**仅为命令与文件准备，
> 不得宣称部署完成**。分类体系（4一级/25二级）不在本指南改动范围。

实际服务名（`deploy/docker-compose.yml`）：`postgres` `redis` `api` `parser-worker`
`ocr-worker` `mineru-worker` `classify-worker`（生产7个）+ `prometheus` `dcgm-exporter`
（`monitoring` profile）。镜像：`Dockerfile.api`（python:3.11-slim，`uvicorn backend.app:get_app
--factory`）、`Dockerfile.ocr`（CUDA11.8+Paddle）、`Dockerfile.mineru`（CUDA12.1+magic-pdf）、
`Dockerfile.infer`（CUDA12.1+torch+BGE/Reranker，LLM走HTTP）。

## 0. Windows本地Git提交推送 → 服务器拉取（当前目录不是git仓库，先建仓）

```powershell
cd "D:\数衍数矩\900万份数据实施方案"
git init; git add -A; git commit -m "pdf classification system: planner→qa-fix, 50 tests green"
git remote add origin <你的Git地址>; git push -u origin main
```

```bash
# 服务器
git clone <你的Git地址> pdfcls && cd pdfcls
git log --oneline -3   # 确认与本地一致
```

## 1. 服务器前置条件

```bash
nvidia-smi  # 驱动≥550，4卡可见
nvcc --version
docker --version && docker compose version
docker run --rm --gpus all nvcr.io/nvidia/cuda:12.1.0-base-ubuntu22.04 nvidia-smi
bash deploy/gpu_check.sh   # PASS全绿、FAIL=0方可继续
```

## 2. 环境变量（`deploy/.env.example` → `deploy/.env`，逐项填写）

| 变量 | 说明 |
|---|---|
| POSTGRES_DB/USER/PASSWORD | 生产随机32位，禁止示例值 |
| API_KEY / LLM_API_KEY | 生产随机值，仓库永不留存 |
| API_URL | 容器内地址，默认`http://api:8000` |
| PDF_ROOT_HOST | 宿主机PDF根目录（容器`:ro`只读挂载3处，不可改） |
| OCR_GPUS=0,1 MINERU_GPUS=2 INFER_GPUS=3 | 与architecture GPU分工一致 |
| BGE_MODEL/RERANKER_MODEL/LLM_ENDPOINT | 真实模型与网关地址 |
| THRESHOLDS_CALIBRATED | **必须=0**，签署后才可改为1（ISSUE-07门禁） |
| CPU_WORKERS | 已废弃（compose不再引用），CPU扩缩用`--scale parser-worker=N` |

`REDIS_MAXMEMORY/API_PORT/PROM_PORT`按服务器内存/端口占用调整。

## 3. 分阶段启动与验证（禁跳步，禁直接全量）

```bash
cd deploy && cp .env.example .env   # 先填好
./ops.sh start                        # 或分阶段：
docker compose up -d postgres redis
docker compose exec -T postgres pg_isready -U $POSTGRES_USER -d $POSTGRES_DB
docker compose exec -T redis redis-cli ping
docker compose up -d api && sleep 8
curl -H "X-API-Key: $API_KEY" http://127.0.0.1:${API_PORT:-8000}/taxonomy
docker compose up -d parser-worker ocr-worker mineru-worker classify-worker
./ops.sh health
```

`classify-worker`启动即校验`API_KEY/API_URL`，缺失直接SystemExit（ISSUE-02）。

## 4. 挂载与持久化验证

```bash
docker inspect pdf-classification-parser-worker-1 --format '{{json .Mounts}}' | grep -o '"RW":false' | wc -l  # ≥1只读
touch /mnt/pdfs/readonly.probe 2>&1  # 宿主机侧确认目录本身不被改写流程触碰（worker只有读权限）
docker compose exec -T redis redis-cli LLEN pdf_tasks
docker exec pdf-classification-ocr-worker-1 ls /data/payloads /data/locks  # workdata共享卷
docker volume inspect pdf-classification_modelcache pdf-classification_pgdata
```

队列只传`file://`引用（<2000B），全文禁入Redis；GPU锁文件`gpu-{ocr,mineru,infer}.lock`
同目录跨进程互斥，崩溃由stale超时回收。

## 5. 20～100份真实PDF端到端试跑

```bash
mkdir -p /tmp/pilot && cp <抽样20-100份>.pdf /tmp/pilot/  # 覆盖文本/扫描/复杂/纯图/无关/边界
# 小批量扫描建任务（limit禁大）：
curl -XPOST -H "X-API-Key: $API_KEY" -H Content-Type:application/json \
  http://127.0.0.1:8000/tasks/scan -d '{"root":"/data/pdfs","limit":50}'
watch -n5 'docker compose exec -T redis redis-cli LLEN pdf_tasks'
curl -H "X-API-Key: $API_KEY" 'http://127.0.0.1:8000/results?size=50' | python3 -m json.tool
# 逐条核对：evidence_mode∈TITLE/BODY/OCR_BODY/VISION、依据带页码、TBD进复核、无Mock痕迹
# （model_version须为真实模型名，出现mock-即停机排查，禁Mock冒充）
```

## 6. 1万份Benchmark与阈值校准流程

```bash
# 6.1 抽样1万份跑全链路，记录分阶段耗时（页/秒分开统计，份/秒与页/秒禁混）：
python3 -c "from scheduler.benchmark import Sample, estimate_full_run;
print(estimate_full_run(Sample(n_files=10000,n_pages=N_PAGES,seconds=T),9000000))"
# 6.2 人工标注金标准集（含无关/边界/无标题/纯图），签署生产阈值；
# 6.3 写入部署配置 THRESHOLDS_CALIBRATED=1 并重启classify-worker；
# 6.4 meets_90d为真才可排工期，否则先扩容/降并发再测。
```

**禁自动启动900万全量任务**：`limit`参数与本节试跑只用于小批量；全量需另行指令。

## 7. 启停与排障速查

`./ops.sh {start|stop|restart|health|logs|ps}`；日志json-file轮转；
`resume_pending`/死信见`ops.sh logs`与README_DEPLOY §5/§7。

## 部署前检查清单

- [ ] gpu_check全绿；驱动/Docker/toolkit就绪
- [ ] .env逐项生产值，THRESHOLDS_CALIBRATED=0
- [ ] PDF目录备份确认，容器只读挂载验证
- [ ] 分阶段启动health三项OK
- [ ] 20～100份试跑人工核对通过，无mock模型版本
- [ ] 1万份Benchmark + 阈值签署完成

## 待配置服务器参数

宿主机PDF根路径、4卡ID映射、PG/Redis密码与备份策略、API/LLM密钥、
BGE/Reranker权重源与LLM网关地址、NVMe工作目录容量、告警接收人。

## 剩余风险

真实GPU依赖未经验证（Paddle/MinerU/torch/FlagEmbedding首次构建可能缺系统库）；
千兆/万兆IO与900万文件扫描耗时未知；OCR与MinerU显存峰值未知，需试跑压测；
prometheus api抓取待backend `/metrics`端点（ISSUE-06剩余项，不影响功能）。
