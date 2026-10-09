# 900万PDF智能分类系统 · 系统API与模块接口设计说明书（V3.0）

版本 V3.0 ｜ 2026-10-09 ｜ 供主管审核、开发交接 ｜ 事实口径：`backend/app.py、schemas.py、models.py、store.py`、`parser/models.py`、classifier 实际返回结构

## 1 接口概述

对外 8 个已实现 HTTP 接口，统一认证 `X-API-Key` 请求头。内部模块接口（Parser→Classification 函数调用、Scheduler→Worker 队列、Backend 持久化函数）单独成章，不与 HTTP 混排。`POST /results/export` 仅见于契约，未实现。

## 2 HTTP 接口总览

| # | 分组 | Method | URL | 功能 |
|---|---|---|---|---|
| 1 | 任务受理 | POST | /tasks/scan | 扫盘建任务，幂等 |
| 2 | 任务查询 | GET | /tasks/{task_id} | 单任务阶段与状态 |
| 3 | 结果查询 | GET | /results | 结果分页筛选 |
| 4 | 结果查询 | GET | /results/{rid} | 结果详情 |
| 5 | 结果写入 | POST | /results | Worker 结果入库 |
| 6 | 人工复核 | GET | /review/queue | 待复核队列 |
| 7 | 人工复核 | POST | /review/{rid} | 复核回写 |
| 8 | 知识库 | GET | /taxonomy | 分类体系查询 |

## 3 核心 API 明细

### 3.1 POST /tasks/scan —— 任务受理

- **功能描述**：扫描目录下 PDF 建任务，重复路径幂等返回旧任务。
- **Method/URL**：POST /tasks/scan，状态码 202。
- **认证**：`X-API-Key`。
- **参数表**：`root: string 必填（目录路径）`；`limit: int 默认1000，范围1–100000`（Pydantic `ScanRequest` 可校验）。
- **完整请求示例**：`{"root": "/data/pdfs", "limit": 1000}`
- **完整响应示例（202）**：`{"accepted": 2, "task_ids": ["9f2c…", "7a11…"]}`
- **HTTP状态码**：202 成功；400 root 非目录；401 鉴权失败。
- **常见异常**：目录不存在、limit 越界（422）。
- **执行流程**：walk 扫盘 → `create_task`（uuid5幂等）→ 返回受理数与任务 ID（异步执行，不等待解析完成）。

### 3.2 GET /tasks/{task_id} —— 任务状态

- **功能描述**：查询单任务所处阶段、运行状态、重试与错误。
- **Method/URL**：GET /tasks/{task_id}，200。
- **认证**：`X-API-Key`。
- **参数表**：`task_id: string 必填（路径参数）`。无请求体，GET 无需请求 JSON。
- **完整响应示例（200）**：`{"task_id": "9f2c…", "file_path": "/data/pdfs/a.pdf", "stage": "PARSE", "status": "RUNNING", "retry_count": 0, "error_code": null, "error_msg": null}`
- **HTTP状态码**：200；404 任务不存在；401。
- **执行流程**：`get_task` 直查 `doc_tasks`。

### 3.3 GET /results —— 结果分页筛选

- **功能描述**：按结果类型/一级类别筛选，分页返回。
- **Method/URL**：GET /results，200。
- **认证**：`X-API-Key`。
- **参数表**：`result_type?: string（5终态之一）`；`level1?: string`；`page: int 默认1 ≥1`；`size: int 默认20，1–200`。无请求体。
- **请求示例**：`GET /results?result_type=CLASSIFIED&page=1&size=20`
- **完整响应示例**：`{"total": 100, "page": 1, "size": 20, "items": [{"id": 1, "result_type": "CLASSIFIED", "level1": "L1_AQSC", "level2": "L2_WHX", "file_path": "/data/pdfs/a.pdf"}]}`
- **状态码**：200；401；422 分页越界。
- **执行流程**：`query_results` 按 id 倒序分页。

### 3.4 GET /results/{rid} —— 结果详情

- **功能描述**：返回证据、分数、决策来源与版本。
- **Method/URL**：GET /results/{rid}，200。
- **认证**：`X-API-Key`。参数 `rid: int 路径参数`。无请求体。
- **完整响应示例**：`{"id": 1, "task_id": "9f2c…", "file_path": "/data/pdfs/a.pdf", "result_type": "CLASSIFIED", "relevance_status": "RELEVANT", "evidence_mode": "BODY", "level1": "L1_AQSC", "level2": "L2_WHX", "candidate_scores": {"L2_WHX": 0.9}, "decision_source": "BGE_DIRECT", "classification_basis": {}, "model_version": "bge-m3-v1", "taxonomy_version": "v1.0-planner"}`
- **状态码**：200；404；401。

### 3.5 POST /results —— 分类结果入库

- **功能描述**：Worker 持久化分类结果，强校验后任务置 SAVE。
- **Method/URL**：POST /results，201。
- **认证**：`X-API-Key`。
- **参数表**：请求体为结果字典，见 §5 字段表。`result_type` 必填（5终态之一）；CLASSIFIED 必带 `level1+level2`，其余四态禁止携带类别（`store.save_result` 校验，Pydantic 校验外加业务校验）。
- **完整请求示例**：`{"task_id": "9f2c…", "file_path": "/data/pdfs/a.pdf", "result_type": "CLASSIFIED", "relevance_status": "RELEVANT", "evidence_mode": "BODY", "level1": "L1_AQSC", "level2": "L2_WHX", "candidate_scores": {"L2_WHX": 0.9}, "decision_source": "BGE_DIRECT"}`
- **完整响应示例（201）**：`{"id": 12}`
- **状态码**：201；400（类型非法/类别携带违规）；401。
- **执行流程**：校验 → 插入 `classification_results` → 任务置 `stage=SAVE,status=DONE`。

### 3.6 GET /review/queue —— 待复核队列

- **功能描述**：列出 `MANUAL_REVIEW 且 reviewed_by 为空` 的分页。
- **Method/URL**：GET /review/queue，200。
- **认证**：`X-API-Key`。参数 `page/size` 同 §3.3。无请求体。
- **完整响应示例**：`{"total": 5, "items": [{"id": 3, "file_path": "/data/pdfs/c.pdf", "level1": null}]}`
- **状态码**：200；401。
- **无人值守说明**：无人复核时记录持久停留队列，不自动升级、不丢弃。

### 3.7 POST /review/{rid} —— 复核回写

- **功能描述**：人工修正类别并记录复核人，仅 MANUAL_REVIEW 可写。
- **Method/URL**：POST /review/{rid}，200。
- **认证**：`X-API-Key`。
- **参数表**：`level1?: string`；`level2?: string`；`reviewer: string 必填非空`；`note: string 默认""`（`ReviewPatch` 可校验）。
- **完整请求示例**：`{"reviewer": "zhang", "level1": "L1_AQSC", "level2": "L2_WHX", "note": "确认"}`
- **完整响应示例**：`{"id": 3, "reviewed_by": "zhang"}`
- **状态码**：200；404（不存在或非 MANUAL_REVIEW）；422（缺 reviewer）；401。

### 3.8 GET /taxonomy —— 分类体系查询

- **功能描述**：返回知识库版本、一级列表与各级二级计数。
- **Method/URL**：GET /taxonomy，200。
- **认证**：`X-API-Key`。无参数、无请求体。
- **完整响应示例**：`{"taxonomy_version": "v1.0-planner", "level1": ["L1_AQSC", "L1_YJJY", "L1_ZHGL", "L1_FZZJ"], "l2_counts": {"L1_AQSC": 7, "L1_YJJY": 3, "L1_ZHGL": 9, "L1_FZZJ": 6}}`
- **说明**：合计 25 二级（7+3+9+6），4 一级不变。

## 4 内部模块接口（非 HTTP）

| 方向 | 接口形式 | 说明 |
|---|---|---|
| Parser→Classification | `ClassificationPipeline.run(parse)` 函数调用 | 输入 ParseResult，输出 ClassificationResult 字典 |
| Scheduler→Worker | Redis 两队列 + 文件引用 | `pdf_tasks`/`classify_tasks` 只传引用；`FilePayloadStore.save/load` 传全文；`Pipeline.run_one/resume_pending` 编排 |
| Backend持久化 | `create_task/get_task/update_task_stage/save_result/query_results/apply_review` | 任务幂等、结果校验入库、分页、复核 |

## 5 完整字段表

### 5.1 ParseResult（`parser/models.py`）

| 字段 | 类型 | 必填/默认 | 说明与示例 |
|---|---|---|---|
| file_path | string | 必填 | 文件路径 |
| pdf_type | string? | 默认 null | TEXT/SCAN/MIXED/COMPLEX/IMAGE 之一 |
| evidence_mode | string? | 默认 null | TITLE/BODY/OCR_BODY/VISION |
| page_count | int | 默认 0 | 页数 |
| title | string | 默认 "" | 标题 |
| headings | array | 默认 [] | 版面标题，最多取前 5 |
| pages | array | 默认 [] | PageContent（page_no/text/spans/image_count/char_count/ocr_score） |
| ocr_mean_score | float? | 默认 null | OCR 平均置信度 |
| ok | bool | 默认 true | 是否成功 |
| error | object? | 默认 null | {code,message,retryable} |
| backend | string | 默认 "" | 实际抽取后端 |
| truncated | bool | 默认 false | 是否截断 |

### 5.2 ClassificationResult / POST /results 请求体（`backend/models.py` + classifier 返回）

| 字段 | 类型 | 必填/默认 | 说明 |
|---|---|---|---|
| task_id | string | 建议携带 | 关联任务，无则不回写阶段 |
| file_path | string | 必填 | 文件路径 |
| file_hash | string | 可选 | 路径哈希 |
| result_type | string | 必填 | CLASSIFIED/IRRELEVANT/MANUAL_REVIEW/FAILED/UNRESOLVED |
| relevance_status | string? | 可选 | RELEVANT/UNCERTAIN/IRRELEVANT |
| evidence_mode | string? | 可选 | 四种证据模式 |
| level1/level2 | string? | CLASSIFIED必填、其余禁止 | 类别 ID |
| candidate_scores | object? | 可选 | 候选项得分；UNRESOLVED 候选一级存于此及 classification_basis，不占 level1/level2（入库约束） |
| decision_source | string? | 可选 | BGE_DIRECT/LLM_JUDGE/MANUAL 等 |
| classification_basis | object? | 可选 | 依据明细 |
| model_version/taxonomy_version/prompt_version | string? | 可选 | 版本 |
| retry_count/error | int/object? | 可选 | 重试与错误 |

### 5.3 ReviewPatch（`backend/schemas.py`，Pydantic 可校验）

| 字段 | 类型 | 必填/默认 | 取值 |
|---|---|---|---|
| reviewer | string | 必填非空 | 复核人 |
| level1/level2 | string? | 默认 null | 修正类别 |
| note | string | 默认 "" | 备注 |

## 6 状态区分：任务运行状态 vs 分类最终状态

- **任务运行状态**（`doc_tasks.status`）：PENDING/RUNNING/DONE/RETRY/DEAD；阶段 `stage` 沿 SCAN→…→SAVE 推进。5 种结果均为终态，入库即置 SAVE/DONE。
- **分类最终状态**（`result_type`）：CLASSIFIED（必须带 L1+L2）、IRRELEVANT（不带类别）、MANUAL_REVIEW（不带类别，进人工队列，`reviewed_by` 为空即待办）、FAILED（不带类别，可重试）、UNRESOLVED（不带类别，候选一级保存在 `candidate_scores`/`classification_basis`，动作为 REPARSE_OR_VISION）。

## 7 代码与契约差异（不掩盖）

| # | 事项 | 结论 |
|---|---|---|
| 1 | POST /results/export | 仅契约存在，代码未实现 |
| 2 | result_states | 契约缺 UNRESOLVED，代码含之，以代码为准 |
| 3 | 分类数量 | 4 一级、25 二级（7+3+9+6），与 taxonomy 一致；其他口径矛盾不采信 |
| 4 | API 示例校验 | ScanRequest/ReviewPatch 可用 Pydantic 直接校验；POST /results 另有业务携带规则校验 |

## QA 核对

8 接口路径/方法/参数/响应已逐项对照源码；GET 无请求体已明确；字段表源自 `parser/models.py`、`backend/models.py/schemas.py` 与 classifier 返回；UNRESOLVED 候选保存与 MANUAL 无人值守语义与 `review.py`/`store.py` 一致；差异事项保留；未编造；未改业务代码与分类规则。
