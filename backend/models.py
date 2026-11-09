from __future__ import annotations
import hashlib
from datetime import datetime
from sqlalchemy import BigInteger, Column, String, Integer, Text, DateTime, JSON, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()


def file_hash_of(path: str) -> str:
    h = hashlib.sha256()
    h.update(path.encode("utf-8"))
    return h.hexdigest()[:32]


class DocTask(Base):
    __tablename__ = "doc_tasks"
    task_id = Column(String(64), primary_key=True)
    file_path = Column(Text, nullable=False)
    file_hash = Column(String(64), nullable=False, index=True)
    stage = Column(String(32), default="SCAN")
    status = Column(String(32), default="PENDING", index=True)
    retry_count = Column(Integer, default=0)
    error_code = Column(String(64), nullable=True)
    error_msg = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ClassificationResult(Base):
    __tablename__ = "classification_results"
    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String(64), index=True)
    file_path = Column(Text, nullable=False)
    file_hash = Column(String(64), index=True)
    result_type = Column(String(32), index=True)
    relevance_status = Column(String(32), nullable=True)
    evidence_mode = Column(String(32), nullable=True)
    level1 = Column(String(32), nullable=True, index=True)
    level2 = Column(String(32), nullable=True, index=True)
    candidate_scores = Column(JSON, nullable=True)
    decision_source = Column(String(32), nullable=True)
    classification_basis = Column(JSON, nullable=True)
    model_version = Column(String(128), nullable=True)
    taxonomy_version = Column(String(64), nullable=True)
    prompt_version = Column(String(64), nullable=True)
    retry_count = Column(Integer, default=0)
    error = Column(JSON, nullable=True)
    reviewed_by = Column(String(128), nullable=True)
    reviewed_level1 = Column(String(32), nullable=True)
    reviewed_level2 = Column(String(32), nullable=True)
    reason = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ArchiveDecision(Base):
    """与 backend/migrations/002、003 一致。PostgreSQL 上以迁移脚本为准，这里只给 SQLite 开发库建表。"""
    __tablename__ = "archive_decisions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    file_id = Column(Text, nullable=False, index=True)
    version = Column(Integer, nullable=False, default=1)
    content_version = Column(Integer, nullable=True)
    archive_type = Column(Text, nullable=False)
    decision_status = Column(Text, nullable=False)
    level1 = Column(Text, nullable=True)
    level2 = Column(Text, nullable=True)
    reason = Column(Text, nullable=True)
    target_dir = Column(Text, nullable=True)
    target_file = Column(Text, nullable=True)
    target_sha256 = Column(String(64), nullable=True)
    source_sha256 = Column(String(64), nullable=True)
    approved = Column(Integer, nullable=False, default=0)


class ArchiveExecution(Base):
    __tablename__ = "archive_executions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    decision_id = Column(Integer, nullable=False, index=True)
    file_id = Column(Text, nullable=False, index=True)
    content_version = Column(Integer, nullable=True)
    tmp_path = Column(Text, nullable=True)
    # 迁移里只有 status=COMPLETED 时 final_path 唯一（部分索引），这里不能整列 unique。
    final_path = Column(Text, nullable=True)
    source_sha256 = Column(String(64), nullable=True)
    target_sha256 = Column(String(64), nullable=True)
    target_bytes = Column(BigInteger, nullable=True)
    bytes_ok = Column(Integer, nullable=False, default=0)
    sha_ok = Column(Integer, nullable=False, default=0)
    src_unchanged = Column(Integer, nullable=False, default=0)
    checksum_verified = Column(Integer, nullable=False, default=0)
    status = Column(Text, nullable=False, default="QUEUED", index=True)
    completed_at = Column(DateTime, nullable=True)


def make_engine(url: str):
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=connect_args, future=True)


def make_session(engine):
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)


def init_db(engine):
    """SQLite（开发、测试）直接建表；PostgreSQL 只跑迁移脚本，绝不 create_all。

    先 create_all 再迁移会建出没有 CHECK、列也对不上的表，002 随后就会失败。
    """
    from phase1 import ledger

    if engine.dialect.name == "sqlite":
        Base.metadata.create_all(engine)
        ledger.metadata.create_all(engine)
        return
    ledger.ensure_schema(engine)
