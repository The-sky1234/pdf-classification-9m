# 任务计划（task-plan.md）

约束：本轮只规划不写业务代码；各Agent按allowed_paths工作；不得并发修改同一文件，冲突用独立worktree。

| 阶段 | 任务 | 负责人 | 输入→输出 | 完成标准 |
|---|---|---|---|---|
| 1 | 业务确认：待确认清单评审，冻结taxonomy v1.0正式版 | planner+业务 | 待确认6项→评审结论 | 无TBD被当绝对规则 |
| 2 | 样本集与标注规范 | qa-reviewer+业务 | 独立标注集 | 覆盖7类必需文件 |
| 3 | 解析与证据包 | parser | architecture/data-contract/state-machine→src/parser,src/evidence | 四类分流+页码证据 |
| 4 | 分类引擎 | classification | taxonomy+证据包→src/classification,src/knowledge | 层级约束+可追溯 |
| 5 | 接口存储复核 | backend | 数据/API/状态机→src/api,models,storage | 契约一致 |
| 6 | 调度可靠性 | scheduler | 架构/状态机→src/scheduler,workers | 幂等断点重试 |
| 7 | 部署监控 | devops | 架构→deploy,ops | 可复现+监控 |
| 8 | 阈值校准+Benchmark | qa-reviewer | 1~5万+10~50万样本 | 阈值签署+16~18页/秒校验 |
| 9 | 试运行→全量 | 全体 | 百万→900万 | 报告齐全 |

文件归属无重叠：contracts归planner；src/parser归parser；src/classification归classification；src/api归backend；src/scheduler归scheduler；deploy归devops；tests/reports归qa（审查只读不改src）。
下一步：先做阶段1业务确认，再按序调度，planner停止后等待用户指令。
