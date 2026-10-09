-- 001_init.sql：PostgreSQL建表（SQLite由SQLAlchemy自动建表，生产执行本脚本）。
CREATE TABLE IF NOT EXISTS doc_tasks (
  task_id VARCHAR(64) PRIMARY KEY,
  file_path TEXT NOT NULL,
  file_hash VARCHAR(64) NOT NULL,
  stage VARCHAR(32) DEFAULT 'SCAN',
  status VARCHAR(32) DEFAULT 'PENDING',
  retry_count INTEGER DEFAULT 0,
  error_code VARCHAR(64),
  error_msg TEXT,
  updated_at TIMESTAMP DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_tasks_path_hash ON doc_tasks (file_hash);
CREATE TABLE IF NOT EXISTS classification_results (
  id SERIAL PRIMARY KEY,
  task_id VARCHAR(64),
  file_path TEXT NOT NULL,
  file_hash VARCHAR(64),
  result_type VARCHAR(32) NOT NULL CHECK (result_type IN ('CLASSIFIED','IRRELEVANT','MANUAL_REVIEW','FAILED','UNRESOLVED')),
  relevance_status VARCHAR(32),
  evidence_mode VARCHAR(32),
  level1 VARCHAR(32),
  level2 VARCHAR(32),
  candidate_scores JSONB,
  decision_source VARCHAR(32),
  classification_basis JSONB,
  model_version VARCHAR(128),
  taxonomy_version VARCHAR(64),
  prompt_version VARCHAR(64),
  retry_count INTEGER DEFAULT 0,
  error JSONB,
  reviewed_by VARCHAR(128),
  reviewed_level1 VARCHAR(32),
  reviewed_level2 VARCHAR(32),
  created_at TIMESTAMP DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_results_type ON classification_results (result_type);
CREATE INDEX IF NOT EXISTS ix_results_l1 ON classification_results (level1);
