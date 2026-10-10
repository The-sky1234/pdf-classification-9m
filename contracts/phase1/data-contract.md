# Phase1 数据契约 v3.0（冻结）

身份修订：2026-10-10。`file_id`、`task_id`、`batch_id`、`content_version` 以 `docs/design/04_数据库详细设计.md`「文件身份规则（已冻结）」及第 1、2、2b、3、4 节为准。同一进程内的毫秒与 `rand_a` 步骤不在本文重写。近重复阈值、归档文件名算法仍 TBD，本文不写数字。

## 身份

FileRecord{file_id,source_path,source_sha256,bytes,mtime,batch_id,content_version,id_origin}

ProcessingTask{task_id,file_id,stage,content_version,task_origin}

- `file_id`：一份源 PDF 的稳定身份，绑定规范路径，不绑定字节。新登记为 RFC 9562 UUIDv7，36 位小写、带连字符，列类型 TEXT，形态 `^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`。只在该规范路径第一次插入 `file_registry` 成功时留下，`id_origin=V7`。`source_path` 冲突则丢弃这枚尚未落库的 ID，读回已有行。
- `task_id`：该 `file_id` 在某个 `stage` 的当前执行行。新行为另一枚 UUIDv7，`task_origin=V7`，且不得等于 `file_id`。`SCAN` 由 Backend 在首次插入该阶段时生成，其他阶段由 Scheduler 在首次插入时生成。唯一约束 `uq_processing_tasks_file_stage` 即 `UNIQUE(file_id, stage)`。失败重试与内容升版只改状态列，不换 `task_id`。
- 历史行：`id_origin=LEGACY`、`task_origin=LEGACY`，列中原文为 32 位小写十六进制。允许 `file_id=task_id`。禁止改写成带连字符的 UUIDv7，禁止回写旧 `doc_tasks.task_id`。读出返回写入时的原文。
- `batch_id`：`processing_batches.id`，数据库 SERIAL。只出现在批次行和 `batch_id` 外键里，不得当作 `file_id`。`file_registry.batch_id` 只记首次登记的批次，之后不改。整批重跑新建 `batch_no`，不得把已有批次的 `cursor_path` 改回空值。
- `source_path`：规范化绝对路径。登记幂等只靠 `uq_file_registry_source_path` 与 `uq_processing_tasks_file_stage`。不靠 Redis `seen`，不靠路径哈希，不靠 uuid5。
- `source_sha256`：文件原始字节的 SHA-256，64 位小写十六进制，可空，非唯一。不同规范路径即使 SHA-256 相同也分别登记。旧 `file_hash`（路径字符串 SHA-256 的前 32 位）禁止写入 `source_sha256`。

应用在插入前生成新 ID。不调用 `uuid.uuid5`、`uuid.uuid4`、`uuid.uuid1`，也不调用 `gen_random_uuid()` 或数据库 `uuidv7()`。

## 内容版本

`content_version` 是源字节版本，记在 `file_registry.content_version` 与 `file_content_versions`。首次确认 64 位字节哈希时插入版本 1，并把已有任务行的 `content_version` 设为 1，不改 `status`、`retry`、`task_id`。已有版本 N 且新哈希不同，才追加 N+1，`file_id` 不变。仅 `mtime` 变化而哈希相同：不升版，更新登记行 `mtime`，任务的 `status` 与 `task_id` 不变。哈希未算完，包括结果仍为空：不插入版本，不得凭缺失哈希升版。已有版本而本次哈希未完成时，登记行的哈希、字节数、`mtime`、`content_version` 都保持不变。

内容从 N 升到 N+1（N≥1）时，同一事务：

1. `stage=SCAN` 的行只把 `content_version` 改为 N+1。`status`、`retry`、`task_id` 不动。
2. 其他阶段已有行改为 `status=PENDING`、`retry=0`、错误列为空、`content_version=N+1`。不删行，不换 `task_id`。
3. 不插入 `archive_decisions`，不把已有 `COMPLETED` 改为 `SUPERSEDED`，不删除目标 PDF，不删除源 PDF。
4. 工作者提交阶段结果时，更新条件带上开始时看到的 `content_version`。影响行数为 0 就丢弃这次结果，不写新的决策行。

## 决策版本与内容版本

第 6、7、9 表的 `version` 是决策版本，对齐同一轮判决，以 `archive_decisions.version` 为准。第 6、7 表没有行时不补假版本。`content_version` 与决策 `version` 不是同一个数，互不代替。

契约要求：`exclusion_decisions`、`classification_results`、`archive_decisions` 必须记下写出该判决时所依据的 `content_version`。04 这三张表的现有列清单没有该列。编码前按本契约补列。本文件不修改 04。

归档执行时，若源文件当前 `content_version` 已大于决策上记录的 `content_version`，不得把该旧决策写成 `COMPLETED`，不得用旧决策覆盖新内容。旧决策保留。新内容走完门控后产生新的决策 `version`。复核改类仍是决策 `version+1`；新副本校验成功后，才把旧执行标 `SUPERSEDED`。

## 队列

`pdf_tasks` 的信封键是 `task_id`。载荷至少包含 `file_id`、`task_id`、`content_version`（尚未确认哈希时为 JSON `null`），并带 `v`。载荷里的 `file_id` 必须等于该 `task_id` 在 `processing_tasks` 中的 `file_id`。登记事务不读、不写 `seen`。`seen` 不是登记幂等。队列丢失或 `seen` 误挡之后，按 `processing_tasks.status=PENDING` 重新投递，不插入第二个 `file_id`。

Worker 开始处理前，比对消息里的 `content_version` 与 `file_registry` 当前 `content_version`（即第 2b 表该文件已提交的最大版本）。两边都为空：不是更旧。消息版本严格更旧则丢弃，不写决策、不复制。

`archive_tasks` 消息带 `file_id`、决策 `version`、该决策所依据的 `content_version`。决策 `version` 更旧则丢弃，不得覆盖更新的决策版本。源内容版本已大于决策所依据的 `content_version` 时，按上一节保留旧决策，不写成 `COMPLETED`。

未知载荷字段忽略。

## 续扫

同一 `batch_no` 续跑使用 `cursor_path`（`COLLATE "C"`），只处理严格大于游标的候选。游标为空则从头处理。

已由冻结规则推出的扫描策略：路径字典序小于游标的新文件，本次续扫会漏掉。已扫过路径的内容变化，游标续扫不会再次打开它。续扫只保证不重复登记已处理路径。要发现游标之前的新路径或已登记路径的内容变化，必须另起一次全量或增量重扫（新 `batch_no`，仍走唯一约束，不产生第二个 `file_id`，不修改 `file_registry.batch_id`）。

## 其余记录

DedupResult{sha256,group_id,is_exact_dup,master_id}（精确可先算，归档仍专利优先。不同路径、相同字节哈希各自保留 `file_id`）。ParseResult见旧data-contract。FeatureResult{query_text≤3000,evidence}。ExclusionDecision{patent:{hit,ev},low_value:{LV01-05,ev},version,content_version}。ClassificationResult沿用+taxonomyv3.0，并带决策 `version` 与所依据的 `content_version`。ArchiveDecision{file_id,version,content_version,archive_type,level1/2,reason,target_dir}。ArchiveExecution{tmp,final,bytes_ok,sha_ok,COMPLETED}。ReviewDecision{version+1,new_target}。上述 `version` 均指决策版本。
