-- 003_registry.sql
-- 依赖 001 与已能独立执行的 002。
-- 建立 processing_batches、file_registry、file_content_versions、processing_tasks，
-- 并把归档决策/执行钉到具体内容版本。
-- 批次表名按数据契约为 processing_batches（不是 ingest_batches）。

CREATE TABLE IF NOT EXISTS processing_batches (
  id SERIAL PRIMARY KEY,
  batch_no TEXT NOT NULL,
  source_roots TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  cursor_path TEXT COLLATE "C",
  scan_status TEXT NOT NULL DEFAULT 'RUNNING',
  files_registered INTEGER NOT NULL DEFAULT 0,
  skipped_count INTEGER NOT NULL DEFAULT 0,
  scan_owner TEXT,
  last_error TEXT,
  CONSTRAINT uq_processing_batches_batch_no UNIQUE (batch_no),
  CONSTRAINT chk_processing_batches_roots CHECK (
    jsonb_typeof(source_roots::jsonb) = 'array'
    AND jsonb_array_length(source_roots::jsonb) >= 1
  ),
  CONSTRAINT chk_processing_batches_status CHECK (
    scan_status IN ('RUNNING', 'INTERRUPTED', 'COMPLETED', 'FAILED')
  ),
  CONSTRAINT chk_processing_batches_counts CHECK (
    files_registered >= 0 AND skipped_count >= 0
  )
);

COMMENT ON TABLE processing_batches IS
  '扫描批次。id 即 batch_id（SERIAL），不得当作 file_id。file_registry.batch_id 只记首次登记批次。';

CREATE TABLE IF NOT EXISTS file_registry (
  file_id TEXT COLLATE "C" PRIMARY KEY,
  source_path TEXT COLLATE "C" NOT NULL,
  source_sha256 CHAR(64),
  "bytes" BIGINT NOT NULL,
  mtime TIMESTAMPTZ NOT NULL,
  batch_id INTEGER NOT NULL,
  content_version INTEGER,
  id_origin TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_file_registry_source_path UNIQUE (source_path),
  CONSTRAINT fk_file_registry_batch FOREIGN KEY (batch_id) REFERENCES processing_batches (id) ON DELETE RESTRICT,
  CONSTRAINT chk_file_registry_id_origin CHECK (id_origin IN ('LEGACY', 'V7')),
  CONSTRAINT chk_file_registry_file_id_form CHECK (
    (id_origin = 'LEGACY' AND file_id ~ '^[0-9a-f]{32}$')
    OR (
      id_origin = 'V7'
      AND file_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
    )
  ),
  CONSTRAINT chk_file_registry_source_sha256 CHECK (
    source_sha256 IS NULL OR source_sha256 ~ '^[0-9a-f]{64}$'
  ),
  CONSTRAINT chk_file_registry_content_version CHECK (
    content_version IS NULL OR content_version >= 1
  )
);

COMMENT ON COLUMN file_registry.file_id IS
  '新 ID 为 UUIDv7 字符串（id_origin=V7）。旧历史 ID 为非 UUIDv7 的 32 位小写十六进制（id_origin=LEGACY）。类型 TEXT，不用 PostgreSQL uuid。格式 CHECK 按 id_origin 分支，不要求旧 ID 通过 UUIDv7。';
COMMENT ON COLUMN file_registry.source_path IS
  '规范化绝对路径。登记幂等只靠 uq_file_registry_source_path。';
COMMENT ON COLUMN file_registry.source_sha256 IS
  '文件字节 SHA-256，可空、非唯一。同哈希不同规范路径可以各有一行。32 位路径哈希不能通过 chk_file_registry_source_sha256。';
COMMENT ON COLUMN file_registry.content_version IS
  '当前已确认的源字节版本指针，可空。历史版本在 file_content_versions，不因本列更新而改写旧版本行或旧归档决策。';

CREATE INDEX IF NOT EXISTS ix_file_registry_source_sha256 ON file_registry (source_sha256);
CREATE INDEX IF NOT EXISTS ix_file_registry_batch_id ON file_registry (batch_id);

CREATE TABLE IF NOT EXISTS file_content_versions (
  file_id TEXT COLLATE "C" NOT NULL,
  content_version INTEGER NOT NULL,
  source_sha256 CHAR(64) NOT NULL,
  "bytes" BIGINT NOT NULL,
  mtime TIMESTAMPTZ NOT NULL,
  batch_id INTEGER NOT NULL,
  observed_at TIMESTAMPTZ NOT NULL,
  CONSTRAINT pk_file_content_versions PRIMARY KEY (file_id, content_version),
  CONSTRAINT fk_file_content_versions_file FOREIGN KEY (file_id) REFERENCES file_registry (file_id) ON DELETE RESTRICT,
  CONSTRAINT fk_file_content_versions_batch FOREIGN KEY (batch_id) REFERENCES processing_batches (id) ON DELETE RESTRICT,
  CONSTRAINT chk_file_content_versions_ver CHECK (content_version >= 1),
  CONSTRAINT chk_file_content_versions_sha CHECK (source_sha256 ~ '^[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS ix_file_content_versions_sha ON file_content_versions (source_sha256);

COMMENT ON TABLE file_content_versions IS
  '源字节版本，只插入。content_version 与 archive_decisions.version 不是同一序列。同一文件可在不同版本重复使用同一哈希，故不对 (file_id, source_sha256) 做唯一约束。';

CREATE OR REPLACE FUNCTION file_content_versions_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'file_content_versions is append-only';
END $$;

DROP TRIGGER IF EXISTS trg_file_content_versions_append_only ON file_content_versions;
CREATE TRIGGER trg_file_content_versions_append_only
  BEFORE UPDATE OR DELETE ON file_content_versions
  FOR EACH ROW
  EXECUTE FUNCTION file_content_versions_append_only();

CREATE TABLE IF NOT EXISTS processing_tasks (
  task_id TEXT COLLATE "C" PRIMARY KEY,
  file_id TEXT COLLATE "C" NOT NULL,
  stage TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'PENDING',
  retry INTEGER NOT NULL DEFAULT 0,
  error_code TEXT,
  error_msg TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  content_version INTEGER,
  task_origin TEXT NOT NULL,
  CONSTRAINT uq_processing_tasks_file_stage UNIQUE (file_id, stage),
  CONSTRAINT fk_processing_tasks_file FOREIGN KEY (file_id) REFERENCES file_registry (file_id) ON DELETE RESTRICT,
  CONSTRAINT fk_processing_tasks_content FOREIGN KEY (file_id, content_version)
    REFERENCES file_content_versions (file_id, content_version) ON DELETE RESTRICT,
  CONSTRAINT chk_processing_tasks_origin CHECK (task_origin IN ('LEGACY', 'V7')),
  CONSTRAINT chk_processing_tasks_task_form CHECK (
    (task_origin = 'LEGACY' AND task_id ~ '^[0-9a-f]{32}$')
    OR (
      task_origin = 'V7'
      AND task_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
    )
  ),
  CONSTRAINT chk_processing_tasks_new_task_ne_file CHECK (task_origin <> 'V7' OR task_id <> file_id),
  CONSTRAINT chk_processing_tasks_status CHECK (status IN ('PENDING', 'RUNNING', 'DONE', 'REVIEW', 'FAILED')),
  CONSTRAINT chk_processing_tasks_content_version CHECK (content_version IS NULL OR content_version >= 1)
);

CREATE INDEX IF NOT EXISTS ix_processing_tasks_status ON processing_tasks (status);

COMMENT ON TABLE processing_tasks IS
  '某文件在某阶段的当前执行行。不是归档指针。content_version 为空时 fk_processing_tasks_content 按 MATCH SIMPLE 不检查。';

ALTER TABLE archive_decisions ALTER COLUMN content_version SET NOT NULL;
ALTER TABLE archive_executions ALTER COLUMN content_version SET NOT NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'uq_archdec_id_file_cv' AND conrelid = 'archive_decisions'::regclass
  ) THEN
    ALTER TABLE archive_decisions
      ADD CONSTRAINT uq_archdec_id_file_cv UNIQUE (id, file_id, content_version);
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'fk_archdec_content' AND conrelid = 'archive_decisions'::regclass
  ) THEN
    ALTER TABLE archive_decisions
      ADD CONSTRAINT fk_archdec_content
      FOREIGN KEY (file_id, content_version)
      REFERENCES file_content_versions (file_id, content_version)
      ON DELETE RESTRICT;
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'fk_archexec_decision_file_cv' AND conrelid = 'archive_executions'::regclass
  ) THEN
    ALTER TABLE archive_executions
      ADD CONSTRAINT fk_archexec_decision_file_cv
      FOREIGN KEY (decision_id, file_id, content_version)
      REFERENCES archive_decisions (id, file_id, content_version)
      ON DELETE RESTRICT;
  END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_archexec_decision_content_completed
  ON archive_executions (decision_id, content_version)
  WHERE status = 'COMPLETED';

COMMENT ON INDEX uq_archexec_decision_content_completed IS
  '同一决策、同一内容版本只允许一条 COMPLETED。一个文件同时只允许一条 COMPLETED，由 one_active_archive 表达。';

CREATE OR REPLACE FUNCTION archive_decisions_identity_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.file_id IS DISTINCT FROM OLD.file_id
     OR NEW.content_version IS DISTINCT FROM OLD.content_version
     OR NEW.version IS DISTINCT FROM OLD.version THEN
    RAISE EXCEPTION 'archive_decisions file_id, version, content_version are immutable';
  END IF;
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_archdec_identity_immutable ON archive_decisions;
CREATE TRIGGER trg_archdec_identity_immutable
  BEFORE UPDATE ON archive_decisions
  FOR EACH ROW
  EXECUTE FUNCTION archive_decisions_identity_immutable();

ALTER TABLE classification_results ADD COLUMN IF NOT EXISTS content_version INTEGER;
ALTER TABLE classification_results ALTER COLUMN file_id TYPE TEXT COLLATE "C";

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'chk_classification_results_content_version'
      AND conrelid = 'classification_results'::regclass
  ) THEN
    ALTER TABLE classification_results
      ADD CONSTRAINT chk_classification_results_content_version
      CHECK (content_version IS NULL OR content_version >= 1);
  END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_classification_results_file_version
  ON classification_results (file_id, version);

COMMENT ON COLUMN classification_results.content_version IS
  '写出该分类行时所依据的源字节版本。旧行可空。与决策 version 不是同一个数。';
