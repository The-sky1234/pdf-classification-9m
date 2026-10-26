-- 004_phase1_loop.sql
-- 补齐应用已经写入、002/003 尚未建出的列和表。
-- 复核记录、精确去重成员，以及归档决策上的结果行指针。

ALTER TABLE archive_decisions ADD COLUMN IF NOT EXISTS result_id INTEGER;

ALTER TABLE file_content_versions ALTER COLUMN observed_at SET DEFAULT now();

CREATE TABLE IF NOT EXISTS review_records (
  id SERIAL PRIMARY KEY,
  result_id INTEGER NOT NULL,
  file_id TEXT,
  reviewer TEXT NOT NULL,
  decision TEXT NOT NULL,
  from_version INTEGER NOT NULL,
  to_version INTEGER NOT NULL,
  level1 TEXT,
  level2 TEXT,
  reason TEXT,
  note TEXT
);

CREATE INDEX IF NOT EXISTS ix_review_records_result ON review_records (result_id);

CREATE TABLE IF NOT EXISTS dedup_members (
  file_id TEXT COLLATE "C" PRIMARY KEY,
  source_sha256 CHAR(64) NOT NULL,
  nbytes BIGINT NOT NULL,
  group_id TEXT NOT NULL,
  master_id TEXT NOT NULL,
  is_exact_dup INTEGER NOT NULL DEFAULT 0,
  CONSTRAINT chk_dedup_exact CHECK (is_exact_dup IN (0, 1)),
  CONSTRAINT chk_dedup_sha CHECK (source_sha256 ~ '^[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS ix_dedup_members_sha ON dedup_members (source_sha256);

COMMENT ON TABLE review_records IS
  '复核人的结论。to_version 是新的决策版本，等于 from_version + 1。';
COMMENT ON TABLE dedup_members IS
  '字节级 SHA-256 相同的成员关系。不决定归档目录。';
