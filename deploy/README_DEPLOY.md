# Linux GPU服务器部署说明（DevOps工程稿；★未在真实服务器验证★）

> 状态声明：本目录所有配置均在 Windows 开发机静态检查通过，**尚未在真实 Linux GPU 服务器上构建/启动/验证**。
> 首次上机按 §6 顺序执行，`gpu_check.sh` 全绿后再 `ops.sh start`。

## 0. 前置（宿主机一次性）

- NVIDIA驱动（≥550）、CUDA 12.1、Docker Engine + `nvidia-container-toolkit`。
-  carried：`bash deploy/gpu_check.sh`，`FAIL=0` 方可继续。
- 兼容性基线：OCR容器 CUDA11.8+PaddlePaddle-GPU 2.6 / MinerU与推理容器 CUDA12.1+torch cu121 /
  MinerU=`magic-pdf[full]` / BGE-M3=`FlagEmbedding` / Reranker=`sentence-transformers`。
  各模型容器相互隔离，不共用依赖。

## 1. 配置

```bash
cp deploy/.env.example deploy/.env   #填写全部CHANGE_ME；.env永不提交
```

`PDF_ROOT_HOST` 指向既有900万PDF根目录；容器内以 `:ro` 只读挂载 `/data/pdfs`，
worker只能读不能写删（另见compose `read_only`说明与§5）。

## 2. 数据库初始化与迁移

- `postgres` 首次启动自动执行 `backend/migrations/001_init.sql`（initdb脚本）。
- 已有库升级：`psql $DATABASE_URL -f backend/migrations/001_init.sql`（脚本幂等，`IF NOT EXISTS`）。
- 生产建议 `pg_dump` 每日备份 `pgdata` 卷。

## 3. 启动/停止/重启/健康检查

```bash
cd deploy
./ops.sh start     # 构建+后台启动全部服务
./ops.sh health    # API+PostgreSQL+Redis+容器状态
./ops.sh logs api  # 查看服务日志
./ops.sh restart classify-worker
./ops.sh stop
```

## 4. 服务矩阵

| 服务 | 角色 | GPU | 说明 |
|---|---|---|---|
| api | FastAPI | 无 | 2 uvicorn workers，限4核/8G |
| parser-worker ×8 | 检测+PyMuPDF | 无 | CPU，可调 `CPU_WORKERS` |
| ocr-worker | PaddleOCR | 0,1 | 独立CUDA11.8容器 |
| mineru-worker | 复杂版式 | 2 | 独立CUDA12.1容器 |
| classify-worker | BGE/Reranker/LLM客户端 | 3 | LLM走HTTP，不内置权重 |
| postgres/redis | 存储/队列 | 无 | 持久卷+AOF，healthy才启动依赖方 |
| prometheus/dcgm | 监控（profile） | — | `docker compose --profile monitoring up -d` |

模型失败策略：缺依赖/超时一律抛明确异常→流水线重试3次→死信/MANUAL_REVIEW，
**禁止回退Mock充数**（`worker_main.py` 无Mock分支）。

## 5. 长期运行可靠性

- 健康检查+`unless-stopped`重启；日志json-file轮转（100~200MB×5）。
- 幂等：任务ID=UUID5(路径+哈希)，重复投递去重；`resume_pending`支持重启后续跑。
- 背压：`Throttle(max_backlog)`+Redis `maxmemory/noeviction`；积压看 `LLEN pdf_tasks`。
- 资源上限：各服务CPU/内存limit；GPU显存由dcgm-exporter+prometheus监控，
  告警阈值：显存>90%、CPU>85%持续10min、磁盘>80%、队列>50000。
- 优雅停止：`ops.sh stop`→SIGTERM→worker处理完当前任务退出并打印graceful-stop。

## 6. 部署验证清单（上机执行）

1. `bash deploy/gpu_check.sh` 全绿。
2. `docker compose config` 无`${...:?}`报错。
3. `./ops.sh start` 后 `./ops.sh health` 三项OK。
4. 投1000份样本→抽查结果/证据/分数→跑 `scheduler.benchmark.estimate_full_run` 核算90天。
5. 断电/重启演练：kill worker后 `resume_pending` 非空且可续跑。

## 7. 问题排查

- `no such table`：pgdata卷残留旧库→`docker compose down -v`（**会清空数据，生产禁用**）后重建。
- OCR报CUDA版本错：确认宿主驱动≥450且容器为Dockerfile.ocr的11.8基线。
- 队列只增不减：`redis-cli LLEN pdf_tasks`，检查worker日志GPU OOM→降并发。
- 分类全进复核：检查`LLM_ENDPOINT`连通性与`modelcache`卷权限。
