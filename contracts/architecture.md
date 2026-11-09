# 系统架构（architecture.md）

单机Linux GPU服务器：Python + FastAPI + PyMuPDF + PaddleOCR + MinerU + BGE-M3 + Reranker + 疑难LLM/Vision + PostgreSQL + Redis/Celery，多Worker并发。

```
主件清单 → CPU抽文字与规则 → 仅扫描页/图片才 PaddleOCR → BGE-M3 + Reranker 选 4/27 → 疑难且显存放得下才有限次 LLM → 复制归档
MinerU 不在这条主路径上。GPU 只有 1 张 RTX A6000 48GB，波次互斥。详见 docs/design/01 第 2 节。
```

## 分层职责
- 解析层解决"有什么"（parser）；Router解决"看什么"（parser）；Gate解决"是否属于体系"（classification）；分层分类解决"属哪类"（classification）；Reranker/LLM解决"模糊怎么办"。
- backend只做接口/存储/复核；scheduler做队列/幂等/断点/GPU协调；devops做容器/CUDA/监控；qa-reviewer做测试/审查/Benchmark。

## 容量基线

按 4 张 GPU 估算的「900 万份 × 10 页、16~18 页/秒」作废，不得再当工期依据。当前机器是 1 张 RTX A6000 48GB。运行方式以 `docs/design/01_系统总体架构设计.md` 第 2 节的波次方案为准。在这张卡上用真实主件 Benchmark 之前，不写吞吐数字。
