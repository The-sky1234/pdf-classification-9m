# 900万PDF智能分类系统 · 系统API与模块接口设计说明书（V3.0）

版本 V3.0｜日期 2026-10-09｜依据：`backend/app.py、schemas.py、store.py、models.py、auth.py、parser/models.py、classifier/pipeline.py、review.py`

## 1 HTTP 接口总览（8 个已实现）

| # | 分组 | Method | URL | 功能 | 认证 |
|---|---|---|---|---|---|
| 1 | 任务受理 | POST | /tasks/scan | 扫盘建任务，幂等 | X-API-Key |
| 2 | 任务查询 | GET | /tasks/{task_id} | 单任务 stage/status | X-API-Key |
| 3 | 结果查询 | GET | /results | 结果分页筛选 | X-API-Key |
| 4 | 结果查询 | GET | /results/{rid} | 结果详情 | X-API-Key |
| 5 | 结果写入 | POST | /results | Worker 入库 | X-API-Key |
| 6 | 人工复核 | GET | /review/queue | 待复核队列 | X-API-Key |
| 7 | 人工复核 | POST | /review/{rid} | 复核回写 | X-API-Key |
| 8 | 知识库 | GET | /taxonomy | 一级列表与二级计数 | X-API-Key |

认证：请求头 `X-API-Key`（`backend/auth.py`，缺失或错误 → 401）。契约中的 `POST /results/export` 在 `app.py` 中无路由，未实现（见第 7 章）。

## 2 核心 API 明细

### 2.1 POST /tasks/scan

功能描述：扫描目录建任务，重复路径幂等返回旧任务。Method/URL：POST /tasks/scan，202 受理。认证：X-API-Key。

参数表（`ScanRequest`）：root string 必填（min_length=1，目录路径）；limit int 可选，默认 1000，范围 1–100000。

完整请求示例：`{"root": "/data/pdfs", "limit": 1000}`

完整响应示例（202）：`{"accepted": 2, "task_ids": ["<uuid5hex>", "<uuid5hex>"]}`

状态码：202 成功；400 root 非目录；401 鉴权失败；422 参数越界。

执行流程：鉴权 → 目录 walk 收 .pdf（至 limit）→ `store.create_task` 逐个幂等建任务 → 返回数量与 task_id。

### 2.2 GET /tasks/{task_id}

功能描述：查询单任务运行状态。Method/URL：GET /tasks/{task_id}。认证：X-API-Key。本接口无请求体。

参数表：task_id string 路径必填。

完整响应示例（200）：`{"task_id": "<hex>", "file_path": "/data/pdfs/a.pdf", "stage": "PARSE", "status": "RUNNING", "retry_count": 0, "error_code": null, "error_msg": null}`

状态码：200；404 任务不存在；401。

### 2.3 GET /results

功能描述：分类结果分页筛选。Method/URL：GET /results。认证：X-API-Key。无请求体。

参数表：result_type string 可选（5 种之一）；level1 string 可选；page int 默认 1（≥1）；size int 默认 20（1–200）。

请求示例：`GET /results?result_type=CLASSIFIED&page=1&size=20`

完整响应示例（200）：`{"total": 100, "page": 1, "size": 20, "items": [{"id": 1, "result_type": "CLASSIFIED", "level1": "L1_AQSC", "level2": "L2_WHX", "file_path": "/data/pdfs/a.pdf"}]}`

状态码：200；401；422 分页越界。

### 2.4 GET /results/{rid}

功能描述：结果详情，含证据、分数、模型与版本。Method/URL：GET /results/{rid}。认证：X-API-Key。无请求体。

完整响应示例（200）：`{"id": 1, "task_id": "<hex>", "file_path": "/data/pdfs/a.pdf", "result_type": "CLASSIFIED", "relevance_status": "RELEVANT", "evidence_mode": "BODY", "level1": "L1_AQSC", "level2": "L2_WHX", "candidate_scores": {"L2_WHX": 0.9}, "decision_source": "BGE_DIRECT", "classification_basis": {"mode": "BODY", "title": "…", "paras": [], "visual": false}, "model_version": "bge-m3-v1", "taxonomy_version": "v1.0-planner"}`

状态码：200；404；401。

### 2.5 POST /results

功能描述：Worker 持久化分类结果，强校验后任务置 SAVE。Method/URL：POST /results，201。认证：X-API-Key。

请求字段（`store.save_result` 实际读取键）：task_id string 可选（关联任务则推进 stage）；file_path string 必填；file_hash string 可选；result_type string 必填（5 种之一）；relevance_status string 可选；evidence_mode string 可选（TITLE/BODY/OCR_BODY/VISION）；level1/level2 string 条件必填；candidate_scores object 可选；decision_source string 可选；classification_basis object 可选；model_version/taxonomy_version/prompt_version string 可选；retry_count int 可选默认 0；error object 可选{code,message,retryable}。

完整请求示例：`{"task_id": "<hex>", "file_path": "/data/pdfs/a.pdf", "result_type": "CLASSIFIED", "relevance_status": "RELEVANT", "evidence_mode": "BODY", "level1": "L1_AQSC", "level2": "L2_WHX", "candidate_scores": {"L2_WHX": 0.9}, "decision_source": "BGE_DIRECT", "classification_basis": {"mode": "BODY", "title": "…", "paras": [], "visual": false}, "model_version": "bge-m3-v1", "taxonomy_version": "v1.0-planner"}`

完整响应示例（201）：`{"id": 12}`

状态码：201；400（result_type 非法 / CLASSIFIED 缺两级 / 非 CLASSIFIED 携带类别）；401。

### 2.6 GET /review/queue

功能描述：列出待人工复核（result_type=MANUAL_REVIEW 且 reviewed_by 为空）。Method/URL：GET /review/queue。认证：X-API-Key。无请求体。

参数表：page 默认 1；size 默认 20（1–200）。

完整响应示例：`{"total": 5, "items": [{"id": 3, "file_path": "/data/pdfs/c.pdf", "level1": null}]}`

### 2.7 POST /review/{rid}

功能描述：人工修正并记录复核人，仅 MANUAL_REVIEW 可写；无人值守时记录一直停留在队列中（reviewed_by 为空），不会自动消失。Method/URL：POST /review/{rid}。认证：X-API-Key。

参数表（`ReviewPatch`）：reviewer string 必填（min_length=1）；level1 string 可选；level2 string 可选；note string 可选默认 ""。

完整请求示例：`{"reviewer": "zhang", "level1": "L1_AQSC", "level2": "L2_WHX", "note": "确认"}`

完整响应示例：`{"id": 3, "reviewed_by": "zhang"}`

状态码：200；404（不存在或非 MANUAL_REVIEW）；422（缺 reviewer）；401。

### 2.8 GET /taxonomy

功能描述：返回知识库版本、一级列表、每级二级计数。Method/URL：GET /taxonomy。认证：X-API-Key。无请求体。

完整响应示例：`{"taxonomy_version": "v1.0-planner", "level1": ["L1_AQSC", "L1_YJJY", "L1_ZHGL", "L1_FZZJ"], "l2_counts": {"L1_AQSC": 7, "L1_YJJY": 3, "L1_ZHGL": 9, "L1_FZZJ": 6}}`

说明：合计 25 个二级（7+3+9+6），与 `taxonomy.yaml` 一致；4 个一级不变。

## 3 模块内部接口（与 HTTP 分开）

| 方向 | 接口 | 说明 |
|---|---|---|
| Parser→Classification | `ClassificationPipeline.run(parse)` | 输入 ParseResult 字典，输出 ClassificationResult 字典 |
| Parser 内部 | `parse_pdf / decide_type / route` | 文件→类型→后端与证据模式 |
| Scheduler→Worker | `RedisQueue.enqueue/dequeue`、`FilePayloadStore.save/load`、`Pipeline.run_one/resume_pending` | 只传 task_id 与 parse_ref 引用 |
| Backend 持久化 | `create_task/get_task/update_task_stage/save_result/query_results/apply_review` | 幂等建模、校验入库、分页、复核 |

## 4 数据契约字段表

ParseResult（`parser/models.py`）：file_path string 必填；pdf_type string 可选（5 种）；evidence_mode string 可选（4 种）；page_count int 默认 0；title string 默认 ""；headings/pages list 默认 []；ocr_mean_score float 可选；ok bool 默认 true；error object 可选{code,message,retryable}；backend string 默认 ""；truncated bool 默认 false。

ClassificationResult（`backend/models.py` + pipeline/review 实际返回）：task_id、file_path、result_type 必填；relevance_status（RELEVANT/UNCERTAIN/IRRELEVANT）；evidence_mode（4 种）；level1/level2（CLASSIFIED 必填，其余必须为空）；candidate_scores object；decision_source（BGE_DIRECT/LLM_JUDGE/MANUAL）；classification_basis{mode,title,paras[:3],visual}；model_version/taxonomy_version/prompt_version；error{code,message,retryable}；reviewed_by/reviewed_level1/reviewed_level2（复核后）；UNRESOLVED 保留一级、二级置空（`_verify_and_close`、`review` L2 分支），候选保留在 candidates_kept。

## 5 状态机

任务运行状态（`DocTask`）：stage SCAN→…→SAVE；status PENDING/RUNNING/DONE/RETRY/DEAD。分类最终状态（`ClassificationResult.result_type`）：CLASSIFIED、IRRELEVANT、MANUAL_REVIEW、FAILED、UNRESOLVED，全部为终态，写入后任务 stage=SAVE、status=DONE。二者区分：前者描述任务走到哪，后者描述文档判成什么。

## 6 Pydantic 可校验性

可用真实模型校验的示例：`ScanRequest`（2.1 请求）、`ReviewPatch`（2.7 请求）可直接 `model_validate`；其余 GET 无请求体无需校验；`POST /results` 接收 dict，由 `store.save_result` 做业务校验而非 Pydantic，示例已按其读取键逐项核对。

## 7 差异事项（真实保留）

| # | 事项 | 结论 |
|---|---|---|
| 1 | POST /results/export | 仅 `contracts/api-contract.yaml` 有，`app.py` 无路由，未实现 |
| 2 | UNRESOLVED | 代码 `VALID_RESULT_TYPES` 含，契约 `result_states` 缺，以代码为准 |
| 3 | UNRESOLVED 保存 | 保留一级、二级置空，有据（pipeline/review），非编造 |
| 4 | MANUAL_REVIEW 无人值守 | reviewed_by 为空则一直在队列，不自动流转 |
| 5 | GPU | 全部 GPU 链路未真实验证，已标注 |

QA 核查：8 接口路径、方法、参数、响应已对照 `app.py/schemas.py/store.py`；字段表对照 `parser/models.py、models.py`；分类数量对照 `taxonomy.yaml`；示例可通过对应模型或存储校验；未编造字段与接口。
