# 状态机（state-machine.md）v1.1-planner修订

## 终态5种（v1.1新增UNRESOLVED，见Planner变更记录）
CLASSIFIED（已分类）/ IRRELEVANT（无关）/ MANUAL_REVIEW（人工复核，仅特殊保留）/
FAILED（处理失败）/ UNRESOLVED（两次LLM自动复核仍无法判断，不强行归类）

## 阶段流转
SCAN → TYPE_DETECT → PARSE(PyMuPDF|OCR|MinerU) → EVIDENCE(TITLE|BODY|OCR_BODY|VISION) →
GATE(RELEVANT→L1；IRRELEVANT终态；UNCERTAIN→LLM自动复核) →
L1(直接/LLM受限/复核) → L2(Reranker直接/LLM Top3/复核) →
AUTO_REVIEW(低置信/候选冲突/题文冲突/UNCERTAIN→LLM最多2轮，第二轮先补证据；
OCR不足先REPARSE_OR_VISION，不臆测) →
VERIFY(证据一致性；冲突→复核) → SAVE

## 自动复核终局
- 2轮内确定→CLASSIFIED/IRRELEVANT；仍不定→UNRESOLVED（保留候选、依据、模型版本、原因）。
- LLM调用失败（超时/异常，重试耗尽）与业务不确定分开记录：前者error.code=LLM_CALL_FAILED→FAILED，
  后者reason进入UNRESOLVED。不关闭认证、不伪造结果。

## 异常
- 可重试（超时IO/超时/GPU忙/LLM调用失败）→retry_count+1→只重跑当前阶段。
- 不可恢复（损坏/加密不可读/页数为0）→FAILED；OCR差且非图片→MANUAL_REVIEW；纯图视觉不足→MANUAL_REVIEW。
- 每阶段独立记录status；幂等条件：file_hash+内容等。

## Planner变更记录 v1.1（2026-10-08，LLM自动复核优化）
- 新增UNRESOLVED终态；MANUAL_REVIEW退为特殊保留通道，不再是常规终点。
- 4个一级/25个二级分类零改动；阈值仍须真实标注校准（LLM自评禁代验收）。
