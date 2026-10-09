# QA综合验收报告（qa-reviewer；2026-10-08 v1.1 LLM自动复核优化复检）

## 变更说明（Planner v1.1）
- `state-machine.md` / `data-contract.md` 修订：新增UNRESOLVED终态（5终态），
  `suggested_action` / `review_stats` 可选字段；4一级/25二级零改动。
- Backend `store.VALID_RESULT_TYPES/TERMINAL` + 迁移SQL CHECK同步UNRESOLVED；
  Scheduler与结果类型解耦，无需改动（已验证）。

## 复检执行
- `py -m pytest tests -q` → **62 passed，0 failed**（新增11个LLM复核用例+1个UNRESOLVED落库用例）。
- 覆盖：prompt含标题/正文/Top3/定义/边界/父子约束；2轮补证据；新类别伪造被拒；
  OCR不足零调用直转REPARSE_OR_VISION；调用失败（FAILED/LLM_CALL_FAILED/可重试）与
  业务不确定（UNRESOLVED）分离记录；超时/重试/成本统计；父子越界否决；
  管道级冲突自动解决；回写认证；跨进程互斥；续跑幂等；未校准阻断。
- 行为变更（旧断言已按新契约更新）：边界冲突/LLM不确定→UNRESOLVED（候选保留）；
  embedding失败→FAILED/EMBEDDING_FAILED；MANUAL_REVIEW仅保留校准门禁与特殊通道。

## 通过 / 失败 / 未测试
- 通过：62项；主流程（Gate/BGE/Reranker）保留；阈值仍须真实标注校准。
- 失败：0。
- 未测试：真实LLM网关2轮裁决、真实OCR重解/Vision链路、真实PG/Redis/GPU、
  1万份Benchmark（同前）。

## 上线结论
代码门禁保持通过；生产仍需首批标注签署+上机实测。**Mock≠准确率，不得承诺工期，
不启动900万全量。**
