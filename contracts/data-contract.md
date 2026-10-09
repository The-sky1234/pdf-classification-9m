# 数据契约（data-contract.md）

## 结果记录（PostgreSQL，建议表 classification_results）
file_path（原只读路径）/ file_hash（可选指纹，幂等键之一）/ result_type=CLASSIFIED|IRRELEVANT|MANUAL_REVIEW|FAILED|UNRESOLVED（v1.1新增） / relevance_status=RELEVANT|IRRELEVANT|UNCERTAIN / evidence_mode=TITLE|BODY|OCR_BODY|VISION / level1/level2（非CLASSIFIED可空，须满足taxonomy父子约束）/ candidate_scores（相关性/一级/二级BGE与Reranker原始分排序、margin）/ decision_source（规则/BGE_DIRECT/RERANKER_DIRECT/LLM_JUDGE/MANUAL）/ classification_basis（页码级原文证据+推断摘要）/ model_version、taxonomy_version、prompt_version / task_status、retry_count、error / suggested_action（v1.1新增可选：REPARSE_OR_VISION等）/ review_stats（v1.1新增可选：LLM调用次数/超时/重试/成本单位）。

## 解析中间（可为表或JSONB）
page/text/bbox/ocr_score、标题层级、evidence_mode、Title Information Score输入特征。不存PDF副本；临时转图用后清理。

## 任务记录（表 doc_tasks）
task_id / file_path+file_hash唯一 / stage（SCAN/TYPE/PARSE/EVIDENCE/GATE/L1/L2/VERIFY/SAVE）/ status / retry_count / error_code（可重试 vs 不可恢复）/ updated_at。失败只重跑当前阶段；完成即幂等跳过。

## 约束
- 原PDF只读；仅存轻量化结果；小规模验证可先CSV/Excel，生产用PostgreSQL。
- 高置信自动通过须多路一致（证据质量/BGE/margin/Reranker/规则/正文校验/LLM一致性），不直接采信LLM自报分数。
- VISION_MODE自动通过阈值高于文本模式。
