# 900万PDF智能分类系统 · 系统API与模块接口设计说明书

版本 v2.0 ｜ 2026-10-09 ｜ 审阅对象：主管、技术负责人 ｜ 口径：以`backend/app.py、schemas.py、store.py`实际代码为准

## 1 接口概述

对外提供8个已实现FastAPI接口（认证均为请求头`X-API-Key`，见`backend/auth.py:require_key`），覆盖任务受理、状态查询、结果查询、结果写入、人工复核、分类体系查询。`POST /results/export`仅存在于契约，未实现。内部另有Parser→Classification函数接口、Scheduler→Worker队列接口、Backend持久化函数接口，与HTTP接口分开描述。

## 2 HTTP接口总览

| 分组 | Method | URL | 功能 | 状态码 |
|---|---|---|---|---|
| 任务受理 | POST | /tasks/scan | 扫盘建任务（幂等） | 202/400/401 |
| 任务查询 | GET | /tasks/{task_id} | 任务状态 | 200/404/401 |
| 结果查询 | GET | /results | 结果分页筛选 | 200/401 |
| 结果查询 | GET | /results/{rid} | 结果详情 | 200/404/401 |
| 结果写入 | POST | /results | 分类结果入库 | 201/400/401 |
| 人工复核 | GET | /review/queue | 复核队列 | 200/401 |
| 人工复核 | POST | /review/{rid} | 复核回写 | 200/404/401/422 |
| 知识库 | GET | /taxonomy | 分类体系查询 | 200/401 |

## 3 核心API明细

### 3.1 POST /tasks/scan（任务受理）

- 用途：扫描目录建任务，重复路径幂等返回旧任务。
- 认证：X-API-Key。
- 参数表：`root(string,必填,目录)`，`limit(int,默认1000,1–100000)`（`schemas.ScanRequest`）。
- 请求JSON：`{"root": "/data/pdfs", "limit": 1000}`
- 响应JSON(202)：`{"accepted": 2, "task_ids": ["abc123", "def456"]}`
- 异常：400（root非目录）、401（鉴权失败）。

### 3.2 GET /tasks/{task_id}（任务状态）

- 用途：查询单任务stage/status/重试与错误。
- 认证：X-API-Key。参数：`task_id`路径参数。
- 响应JSON(200)：`{"task_id": "abc123", "file_path": "/data/pdfs/a.pdf", "stage": "PARSE", "status": "RUNNING", "retry_count": 0, "error_code": null, "error_msg": null}`
- 异常：404（任务不存在）、401。

### 3.3 GET /results（结果分页）

- 用途：按result_type/level1筛选分页。
- 认证：X-API-Key。参数：`result_type?, level1?, page≥1默认1, size 1–200默认20`。
- 请求示例：`GET /results?result_type=CLASSIFIED&page=1&size=20`
- 响应JSON：`{"total": 100, "page": 1, "size": 20, "items": [{"id": 1, "result_type": "CLASSIFIED", "level1": "L1_AQSC", "level2": "L2_WHX", "file_path": "/data/pdfs/a.pdf"}]}`
- 异常：401、422（分页越界）。

### 3.4 GET /results/{rid}（结果详情）

- 用途：返回证据、分数、模型与版本。
- 认证：X-API-Key。
- 响应JSON：`{"id": 1, "task_id": "abc123", "file_path": "/data/pdfs/a.pdf", "result_type": "CLASSIFIED", "relevance_status": "RELEVANT", "evidence_mode": "BODY", "level1": "L1_AQSC", "level2": "L2_WHX", "candidate_scores": {"L2_WHX": 0.9}, "decision_source": "BGE_DIRECT", "classification_basis": "…", "model_version": "bge-m3-v1", "taxonomy_version": "v1.0-planner"}`
- 异常：404、401。

### 3.5 POST /results（结果入库）

- 用途：Worker持久化分类结果，强校验后任务置SAVE。
- 认证：X-API-Key。请求为结果字典（见§5数据契约）。
- 请求JSON：`{"task_id": "abc123", "file_path": "/data/pdfs/a.pdf", "result_type": "CLASSIFIED", "level1": "L1_AQSC", "level2": "L2_WHX", "evidence_mode": "BODY"}`
- 响应JSON(201)：`{"id": 12}`
- 异常：400（result_type非法 / CLASSIFIED缺L1+L2 / 非CLASSIFIED携带类别）、401。

### 3.6 GET /review/queue（复核队列）

- 用途：列出`MANUAL_REVIEW且未复核`分页。
- 认证：X-API-Key。参数：`page, size`同上。
- 响应JSON：`{"total": 5, "items": [{"id": 3, "file_path": "/data/pdfs/c.pdf", "level1": null}]}`
- 异常：401。

### 3.7 POST /review/{rid}（复核回写）

- 用途：人工修正类别并记录复核人，仅MANUAL_REVIEW可写。
- 认证：X-API-Key。请求体`ReviewPatch{level1?, level2?, reviewer(必填), note=""}`。
- 请求JSON：`{"reviewer": "zhang", "level1": "L1_AQSC", "level2": "L2_WHX", "note": "确认"}`
- 响应JSON：`{"id": 3, "reviewed_by": "zhang"}`
- 异常：404（不存在或非MANUAL_REVIEW）、422（缺reviewer）、401。

### 3.8 GET /taxonomy（分类体系）

- 用途：返回知识库版本、一级列表、每级二级计数（`classifier/knowledge.load_taxonomy`）。
- 认证：X-API-Key。
- 响应JSON：`{"taxonomy_version": "v1.0-planner", "level1": ["L1_AQSC", "L1_YJJY", "L1_ZHGL", "L1_FZZJ"], "l2_counts": {"L1_AQSC": 7, "L1_YJJY": 3, "L1_ZHGL": 9, "L1_FZZJ": 6}}`
- 说明：合计25个二级（7+3+9+6），与`taxonomy.yaml`一致。

## 4 模块内部接口

| 方向 | 接口 | 输入→输出 |
|---|---|---|
| Parser→Classification | `ClassificationPipeline.run(parse: ParseResult)` | ParseResult→ClassificationResult |
| Parser内部 | `parse_pdf(path,…)/detect_only`，`decide_type`，`route(pdf_type,has_title)` | 文件→类型→后端+证据模式 |
| Scheduler→Worker | `RedisQueue.enqueue/dequeue`（pdf_tasks/classify_tasks，仅引用），`FilePayloadStore.save/load(parse_ref)`，`Pipeline.run_one/resume_pending` | task_id引用流转 |
| Backend持久化 | `store.create_task/get_task/update_task_stage/save_result/query_results/apply_review` | 任务幂等建模、结果校验入库、分页、复核 |

## 5 数据契约

- ParseResult：`{ok, file_path, pdf_type, evidence_mode, pages[{title,headings,paras}], error{code,message,retryable}}`。
- EvidencePackage：`{mode(TITLE/BODY/OCR_BODY/VISION), bundle{title,headings[:5],paras}, query_text}`。
- ClassificationResult：`{task_id,file_path,result_type,relevance_status,evidence_mode,level1,level2,candidate_scores,decision_source,classification_basis,model_version,taxonomy_version,prompt_version,review_*}`。
- 结果契约：CLASSIFIED必须带L1+L2；IRRELEVANT/MANUAL_REVIEW/UNRESOLVED/FAILED不得带类别；5种均为终态，写入后任务`stage=SAVE,status=DONE`（`store.TERMINAL`）。

## 6 错误处理与状态机

- 认证：缺失/错误X-API-Key→401（`require_key`）。
- 错误码：400（参数/校验）、404（任务/结果/复核项不存在）、422（分页越界、缺reviewer）。
- 任务状态机：`stage: SCAN→…→SAVE`，`status: PENDING/RUNNING/DONE/RETRY/DEAD`；解析错误`ZERO_PAGES/ENCRYPTED/CORRUPT`不可重试，`OCR_FAILED/MINERU_FAILED/RETRYABLE_IO/TIMEOUT/GPU_BUSY`可重试（≤3次）。
- 分页：`page≥1, size 1–200`，`total/page/size/items`统一信封。

## 7 差异事项（QA核定）

| # | 检查项 | 结论 |
|---|---|---|
| 1 | 一级/二级数量 | 真实为4个一级、25个二级（7+3+9+6）；"安全生产12类、应急救援8类"与`taxonomy.yaml`及`GET /taxonomy`矛盾，不予采信，未改分类体系 |
| 2 | POST /results/export | 仅存在于`contracts/api-contract.yaml`，`backend/app.py`无此路由，未实现 |
| 3 | MANUAL_REVIEW/UNRESOLVED | `store.VALID_RESULT_TYPES`含二者；契约`result_states`缺UNRESOLVED，以代码为准 |
| 4 | 单机GPU部署 | 仅完成Compose配置与指南，未真实部署验证，不宣称已上线 |

QA检查：8个接口的路径、方法、参数、响应字段已逐项对照`app.py/schemas.py/store.py`；分类数量对照`taxonomy.yaml`与`knowledge.load_taxonomy`；未发现新增编造接口。发现问题见上表。
