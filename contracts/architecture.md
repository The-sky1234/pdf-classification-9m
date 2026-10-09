# 系统架构（architecture.md）

单机Linux GPU服务器：Python + FastAPI + PyMuPDF + PaddleOCR + MinerU + BGE-M3 + Reranker + 疑难LLM/Vision + PostgreSQL + Redis/Celery，多Worker并发。

```
既有PDF(只读) → 扫描/任务生成 → 类型检测 → ┌PyMuPDF(文本) ┐
                                              ├PaddleOCR(扫描)┤→ 统一结构 → Evidence Router → Relevance Gate → 一级(规则+BGE+LLM) → 二级(BGE Top5→Reranker→LLM Top3) → 一致性校验 → PostgreSQL
                                              └MinerU(复杂) ┘                                              ↘ IRRELEVANT / MANUAL_REVIEW / FAILED
                     Redis/Celery + Parser/OCR/MinerU/Classifier Worker + GPU分工(GPU0/1 OCR, GPU2 MinerU, GPU3 BGE/Reranker/LLM-Vision) + FastAPI + 复核/导出
```

## 分层职责
- 解析层解决"有什么"（parser）；Router解决"看什么"（parser）；Gate解决"是否属于体系"（classification）；分层分类解决"属哪类"（classification）；Reranker/LLM解决"模糊怎么办"。
- backend只做接口/存储/复核；scheduler做队列/幂等/断点/GPU协调；devops做容器/CUDA/监控；qa-reviewer做测试/审查/Benchmark。

## 容量基线（预算假设，非承诺）
900万份×10页≈9000万页；文本50%/扫描35%/复杂10%/纯图5%；90天需综合16~18页/秒（70~75%利用率校核）。正式工期以1万份真实Benchmark为准。

## 生产基线（预算级）
64~96核CPU / 512GB ECC / 4×48GB GPU / 系统1TB + 工作4~8TB NVMe / 10GbE起步。验证型与可运行型降配见docs硬件报告。
