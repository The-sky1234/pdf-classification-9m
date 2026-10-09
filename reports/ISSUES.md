# 问题清单（qa-reviewer签发；2026-10-08修复轮复检更新）

| 编号 | 级别 | 所属模块 | 问题 | 修复证据 | 状态 |
|---|---|---|---|---|---|
| ISSUE-01 | 阻塞 | scheduler+backend | 跨Worker交接缺失 | 新增`scheduler/handoff.py`（FilePayloadStore文件引用+record续跑，引用<2000B）；`deploy/worker_main.py`parser/ocr/mineru角色解析→持久化→转发`:classify`交接，classify角色消费引用→分类→回写；`backend/store.update_task_stage`支持阶段推进；`test_handoff.py`3用例+`test_qa_verify`真实交接用例通过 | 已关闭 |
| ISSUE-02 | 阻塞 | deploy | classify-worker缺鉴权变量 | compose补`API_URL/API_KEY`（`?required`缺失即报错）；`worker_main.require_env`缺失即SystemExit；`test_worker_cfg.py`2用例通过 | 已关闭 |
| ISSUE-03 | 严重 | classification | evidence_mode与契约不一致 | 判定：data-contract与state-machine一致要求TITLE四值，错在classifier，无需Planner改契约；`evidence.py`重写为契约四值（兼容旧`_MODE`输入）；e2e收紧为严格断言；`test_contract_mode`通过 | 已关闭 |
| ISSUE-04 | 严重 | backend | 非CLASSIFIED可带类别 | `store.save_result`改为非CLASSIFIED带类别一律400；`test_guards`覆盖IRRELEVANT/MANUAL_REVIEW | 已关闭 |
| ISSUE-05 | 一般 | classifier | evidence死代码 | `generic_only`死表达式删除，改为`_is_generic_title`单函数；契约模式用例通过 | 已关闭 |
| ISSUE-06 | 一般 | deploy | replicas/prometheus误配 | replicas改为`--scale`注释；`prometheus.yml`删除错误metrics_path并注明backend需补`/metrics`（剩余跟进项） | 部分关闭（/metrics端点待backend排期） |
| ISSUE-07 | 阻塞（流程） | classification | Mock阈值禁用于生产 | `Thresholds.calibrated`默认False；`pipeline._auto`未校准时自动终态一律降MANUAL_REVIEW（reason=uncalibrated_thresholds_blocked）；`test_guards`阻断/放行双用例通过；生产签署后以`THRESHOLDS_CALIBRATED=1`显式启用 | 已关闭（门禁生效；生产值待签署） |
| ISSUE-08 | 知悉 | contracts | classification-rules.md缺失 | 不编造，现行以requirements+taxonomy为准 | 保持知悉 |
| ISSUE-09 | 严重 | scheduler/deploy | GPU互斥名存实亡 | 新增`scheduler/gpu_lock.py`文件锁O_EXCL+stale超时回收；`worker_main`三处模型调用持锁（ocr/mineru/infer）；`test_gpu_lock`跨进程互斥+崩溃回收通过 | 已关闭 |
| ISSUE-10 | 一般 | backend | 导入副作用建库 | `app=build_app()`改为`get_app()`工厂+`__main__`入口；`Dockerfile.api`改`--factory`；`test_guards`子进程导入无dev.db | 已关闭 |

修复纪律：先测试后代码；无绕过断言；目录归属零交叉（scheduler/backend/classifier/deploy各改各文件）；
4一级/25二级未动；无PDF写操作；Mock均标注非生产验证。

| ISSUE-11 | 严重→已关闭 | classification | LLM自动复核缺失，MANUAL常规终点无人消费 | v1.1：新增classifier/review.py（2轮+补证据+OCR重解+超时重试成本），pipeline全冲突路径接入；test_llm_review.py 11用例通过 |
| ISSUE-12 | 流程→已关闭 | contracts/backend | UNRESOLVED状态缺失 | Planner v1.1修订state-machine/data-contract；backend store+迁移SQL同步；scheduler解耦无需改 |
