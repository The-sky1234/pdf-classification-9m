-- 002_archive.sql
-- 本文件此前未入库，测试库上一次执行因 archive_decisions 不存在而整段回滚。
-- 这里改为先创建归档决策/执行表，再补列、唯一索引和 CHECK，语句可重入。
-- 重复执行的权威保护是 schema_migrations，不靠静默忽略错误。
-- file_id 使用 TEXT，不用 PostgreSQL uuid。新 ID 为 UUIDv7 字符串；
-- 旧历史 ID 可为非 UUIDv7 字符串。本脚本不对 file_id 加“必须是 UUIDv7”的 CHECK。

CREATE TABLE IF NOT EXISTS archive_decisions (
  id SERIAL PRIMARY KEY,
  file_id TEXT COLLATE "C" NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  content_version INTEGER,
  archive_type TEXT NOT NULL,
  decision_status TEXT NOT NULL,
  level1 TEXT,
  level2 TEXT,
  reason TEXT,
  target_dir TEXT,
  target_file TEXT,
  target_sha256 CHAR(64),
  source_sha256 CHAR(64),
  approved INTEGER NOT NULL DEFAULT 0,
  CONSTRAINT chk_archdec_type CHECK (archive_type IN ('PATENT', 'LOW_VALUE', 'BUSINESS', 'PENDING')),
  CONSTRAINT chk_archdec_decision_status CHECK (decision_status IN ('APPROVED', 'NEED_REVIEW')),
  CONSTRAINT chk_archdec_approved CHECK (approved IN (0, 1)),
  CONSTRAINT chk_archdec_content_version CHECK (content_version IS NULL OR content_version >= 1)
);

COMMENT ON TABLE archive_decisions IS
  '某一决策版本的唯一主目录。version 是决策版本。content_version 是该判决所依据的源字节版本，二者不是同一序列。';
COMMENT ON COLUMN archive_decisions.file_id IS
  'TEXT 字符串。新 ID 为 UUIDv7（36 位小写、带连字符）。旧历史 ID 可为非 UUIDv7 字符串（既有台账为 32 位小写十六进制）。禁止改成 PostgreSQL uuid。';
COMMENT ON COLUMN archive_decisions.content_version IS
  '写出该决策时所依据的 file_content_versions.content_version。003 设为 NOT NULL 并外键到该版本行，不指向会被后续扫描改写的 processing_tasks 当前行。';
COMMENT ON COLUMN archive_decisions.version IS
  '决策版本。UNIQUE(file_id, version)。复核改类写 version+1，不覆盖本行。';

ALTER TABLE archive_decisions ADD COLUMN IF NOT EXISTS reason TEXT;
ALTER TABLE archive_decisions ADD COLUMN IF NOT EXISTS target_dir TEXT;
ALTER TABLE archive_decisions ADD COLUMN IF NOT EXISTS source_sha256 CHAR(64);
ALTER TABLE archive_decisions ADD COLUMN IF NOT EXISTS approved INTEGER DEFAULT 0;
ALTER TABLE archive_decisions ADD COLUMN IF NOT EXISTS content_version INTEGER;
ALTER TABLE archive_decisions ADD COLUMN IF NOT EXISTS decision_status TEXT;
ALTER TABLE archive_decisions ADD COLUMN IF NOT EXISTS target_file TEXT;
ALTER TABLE archive_decisions ADD COLUMN IF NOT EXISTS target_sha256 CHAR(64);
ALTER TABLE archive_decisions ADD COLUMN IF NOT EXISTS archive_type TEXT;
ALTER TABLE archive_decisions ADD COLUMN IF NOT EXISTS level1 TEXT;
ALTER TABLE archive_decisions ADD COLUMN IF NOT EXISTS level2 TEXT;

CREATE TABLE IF NOT EXISTS archive_executions (
  id SERIAL PRIMARY KEY,
  decision_id INTEGER NOT NULL,
  file_id TEXT COLLATE "C" NOT NULL,
  content_version INTEGER,
  tmp_path TEXT,
  final_path TEXT,
  source_sha256 CHAR(64),
  target_sha256 CHAR(64),
  target_bytes BIGINT,
  bytes_ok INTEGER NOT NULL DEFAULT 0,
  sha_ok INTEGER NOT NULL DEFAULT 0,
  src_unchanged INTEGER NOT NULL DEFAULT 0,
  checksum_verified INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'QUEUED',
  completed_at TIMESTAMPTZ,
  CONSTRAINT fk_archexec_decision FOREIGN KEY (decision_id) REFERENCES archive_decisions (id) ON DELETE RESTRICT,
  CONSTRAINT chk_archexec_status CHECK (status IN ('QUEUED', 'COPYING', 'VERIFYING', 'COMPLETED', 'FAILED', 'SUPERSEDED')),
  CONSTRAINT chk_archexec_flags CHECK (
    bytes_ok IN (0, 1)
    AND sha_ok IN (0, 1)
    AND src_unchanged IN (0, 1)
    AND checksum_verified IN (0, 1)
  ),
  CONSTRAINT chk_completed CHECK (
    status <> 'COMPLETED'
    OR (
      bytes_ok = 1
      AND sha_ok = 1
      AND src_unchanged = 1
      AND final_path IS NOT NULL
      AND btrim(final_path) <> ''
      AND target_bytes IS NOT NULL
      AND target_bytes >= 0
      AND target_sha256 IS NOT NULL
      AND target_sha256 ~ '^[0-9a-f]{64}$'
      AND source_sha256 IS NOT NULL
      AND source_sha256 ~ '^[0-9a-f]{64}$'
    )
  ),
  CONSTRAINT chk_archexec_content_version CHECK (content_version IS NULL OR content_version >= 1)
);

COMMENT ON TABLE archive_executions IS
  '某一决策版本的复制与校验记录。COMPLETED 必须有目标实体路径、字节大小和 sha256，仅有状态标签不算归档。';
COMMENT ON COLUMN archive_executions.content_version IS
  '本执行所依据的源字节版本，003 用复合外键钉死到对应 archive_decisions 的 file_id 与 content_version。';
COMMENT ON COLUMN archive_executions.target_bytes IS
  '目标 PDF 实体的字节大小。COMPLETED 缺少路径、target_bytes 或 sha256 时 chk_completed 拒绝。';
COMMENT ON COLUMN archive_executions.tmp_path IS
  '同盘临时路径。正式完成只认 final_path 上校验通过的实体。';

ALTER TABLE archive_executions ADD COLUMN IF NOT EXISTS tmp_path TEXT;
ALTER TABLE archive_executions ADD COLUMN IF NOT EXISTS source_sha256 CHAR(64);
ALTER TABLE archive_executions ADD COLUMN IF NOT EXISTS target_sha256 CHAR(64);
ALTER TABLE archive_executions ADD COLUMN IF NOT EXISTS target_bytes BIGINT;
ALTER TABLE archive_executions ADD COLUMN IF NOT EXISTS bytes_ok INTEGER DEFAULT 0;
ALTER TABLE archive_executions ADD COLUMN IF NOT EXISTS sha_ok INTEGER DEFAULT 0;
ALTER TABLE archive_executions ADD COLUMN IF NOT EXISTS src_unchanged INTEGER DEFAULT 0;
ALTER TABLE archive_executions ADD COLUMN IF NOT EXISTS checksum_verified INTEGER DEFAULT 0;
ALTER TABLE archive_executions ADD COLUMN IF NOT EXISTS content_version INTEGER;
ALTER TABLE archive_executions ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;
ALTER TABLE archive_executions ADD COLUMN IF NOT EXISTS final_path TEXT;
ALTER TABLE archive_executions ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'QUEUED';

ALTER TABLE classification_results ADD COLUMN IF NOT EXISTS file_id VARCHAR(64);
ALTER TABLE classification_results ADD COLUMN IF NOT EXISTS version INTEGER DEFAULT 1;
ALTER TABLE classification_results ADD COLUMN IF NOT EXISTS target_path TEXT;
ALTER TABLE classification_results ADD COLUMN IF NOT EXISTS archive_status VARCHAR(32) DEFAULT 'NONE';

CREATE UNIQUE INDEX IF NOT EXISTS uq_archdec_fv ON archive_decisions (file_id, version);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_archive ON archive_executions (file_id) WHERE status = 'COMPLETED';
CREATE UNIQUE INDEX IF NOT EXISTS one_final_completed ON archive_executions (final_path) WHERE status = 'COMPLETED' AND final_path IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_archexec_status ON archive_executions (status);
CREATE INDEX IF NOT EXISTS ix_archexec_decision ON archive_executions (decision_id);

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'chk_completed'
      AND conrelid = 'archive_executions'::regclass
  ) THEN
    ALTER TABLE archive_executions ADD CONSTRAINT chk_completed CHECK (
      status <> 'COMPLETED'
      OR (
        bytes_ok = 1
        AND sha_ok = 1
        AND src_unchanged = 1
        AND final_path IS NOT NULL
        AND btrim(final_path) <> ''
        AND target_bytes IS NOT NULL
        AND target_bytes >= 0
        AND target_sha256 IS NOT NULL
        AND target_sha256 ~ '^[0-9a-f]{64}$'
        AND source_sha256 IS NOT NULL
        AND source_sha256 ~ '^[0-9a-f]{64}$'
      )
    );
  END IF;
END $$;

CREATE OR REPLACE VIEW v_legacy_results AS
  SELECT id, task_id AS file_id, result_type, level1, level2, file_path
  FROM classification_results;

COMMENT ON VIEW v_legacy_results IS
  '只读兼容。不把 IRRELEVANT 改写成低价值目录。';
