-- 004_phase1_ledger.sql
-- 阶段一任务台账：每份主件一行，波次之间靠 stage 交接，靠租约并发领取。
-- 字段与 phase1/ledger.py 的 files 表一一对应，改一边必须改另一边。

CREATE TABLE IF NOT EXISTS phase1_files (
  id BIGSERIAL PRIMARY KEY,
  path_key VARCHAR(40) NOT NULL,
  source_path TEXT NOT NULL,
  source_from VARCHAR(32),
  extension VARCHAR(16),
  size_bytes BIGINT,
  stage VARCHAR(16) NOT NULL,
  failed_stage VARCHAR(16),
  lease_owner VARCHAR(80),
  lease_until DOUBLE PRECISION,
  attempts INTEGER NOT NULL DEFAULT 0,
  archive_type VARCHAR(16),
  decision_status VARCHAR(16),
  reason TEXT,
  level1 VARCHAR(16),
  level2 VARCHAR(32),
  target_dir TEXT,
  page_count INTEGER,
  ocr_pages TEXT,
  evidence BYTEA,
  snippet TEXT,
  scores TEXT,
  archive_path TEXT,
  archive_sha256 VARCHAR(64),
  archive_bytes BIGINT,
  previous_archive_path TEXT,
  previous_archive_sha256 VARCHAR(64),
  reviewed_by VARCHAR(64),
  reviewed_at DOUBLE PRECISION,
  review_note TEXT,
  last_error TEXT,
  updated_at DOUBLE PRECISION NOT NULL,
  CONSTRAINT uq_phase1_files_path_key UNIQUE (path_key),
  CONSTRAINT chk_phase1_files_stage CHECK (stage IN ('cpu', 'ocr', 'classify', 'archive', 'done', 'failed')),
  CONSTRAINT chk_phase1_files_type CHECK (
    archive_type IS NULL OR archive_type IN ('PATENT', 'LOW_VALUE', 'BUSINESS', 'PENDING')
  ),
  CONSTRAINT chk_phase1_files_done CHECK (
    stage <> 'done' OR (archive_path IS NOT NULL AND archive_sha256 ~ '^[0-9a-f]{64}$' AND archive_bytes >= 0)
  )
);

CREATE INDEX IF NOT EXISTS ix_phase1_files_stage_id ON phase1_files (stage, id);
CREATE INDEX IF NOT EXISTS ix_phase1_files_type_reason ON phase1_files (archive_type, reason);

COMMENT ON TABLE phase1_files IS
  '阶段一任务台账。path_key = sha1(source_path)，避免超长路径撞 btree 上限。done 行必须带归档路径、sha256 和字节数。';

ALTER TABLE classification_results ADD COLUMN IF NOT EXISTS reason TEXT;
