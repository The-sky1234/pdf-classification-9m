from __future__ import annotations
import hashlib
from datetime import datetime
from sqlalchemy import Column, String, Integer, Text, DateTime, JSON, create_engine, UniqueConstraint
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
    file_id = Column(String(64), nullable=True, index=True)
    content_version = Column(Integer, nullable=True)
    version = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ArchiveDecision(Base):
    __tablename__ = "archive_decisions"
    __table_args__ = (UniqueConstraint("file_id", "version", name="uq_archdec_file_version"),)
    id = Column(Integer, primary_key=True, autoincrement=True)
    file_id = Column(String(64), index=True)
    version = Column(Integer, default=1)
    archive_type = Column(String(32))
    level1 = Column(String(32), nullable=True)
    level2 = Column(String(32), nullable=True)
    reason = Column(String(32), nullable=True)
    target_dir = Column(Text, nullable=True)
    target_file = Column(Text, nullable=True)
    target_sha256 = Column(String(64), nullable=True)
    source_sha256 = Column(String(64), nullable=True)
    approved = Column(Integer, default=0)
    content_version = Column(Integer, nullable=True)
    decision_status = Column(String(32), nullable=True)
    result_id = Column(Integer, nullable=True)


class ArchiveExecution(Base):
    __tablename__ = "archive_executions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    decision_id = Column(Integer, index=True)
    file_id = Column(String(64), index=True)
    tmp_path = Column(Text, nullable=True)
    final_path = Column(Text, nullable=True, unique=True)
    source_sha256 = Column(String(64), nullable=True)
    target_sha256 = Column(String(64), nullable=True)
    bytes_ok = Column(Integer, default=0)
    sha_ok = Column(Integer, default=0)
    src_unchanged = Column(Integer, default=0)
    checksum_verified = Column(Integer, default=0)
    status = Column(String(32), default="QUEUED", index=True)
    content_version = Column(Integer, nullable=True)
    target_bytes = Column(Integer, nullable=True)
    completed_at = Column(DateTime, nullable=True)


class ProcessingBatch(Base):
    __tablename__ = "processing_batches"
    id = Column(Integer, primary_key=True, autoincrement=True)
    batch_no = Column(String(64), nullable=False, unique=True)
    source_roots = Column(Text, nullable=False)
    cursor_path = Column(Text, nullable=True)
    scan_status = Column(String(32), default="RUNNING")
    files_registered = Column(Integer, default=0)
    skipped_count = Column(Integer, default=0)
    last_error = Column(Text, nullable=True)


class FileRegistry(Base):
    __tablename__ = "file_registry"
    file_id = Column(String(64), primary_key=True)
    source_path = Column(Text, nullable=False, unique=True)
    source_sha256 = Column(String(64), nullable=True)
    nbytes = Column("bytes", Integer, nullable=False, default=0)
    mtime = Column(DateTime, nullable=False, default=datetime.utcnow)
    batch_id = Column(Integer, nullable=False, index=True)
    content_version = Column(Integer, nullable=True)
    id_origin = Column(String(16), nullable=False, default="V7")


class FileContentVersion(Base):
    __tablename__ = "file_content_versions"
    file_id = Column(String(64), primary_key=True)
    content_version = Column(Integer, primary_key=True)
    source_sha256 = Column(String(64), nullable=False)
    nbytes = Column("bytes", Integer, nullable=False)
    mtime = Column(DateTime, nullable=False, default=datetime.utcnow)
    observed_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    batch_id = Column(Integer, nullable=False)


class ProcessingTask(Base):
    __tablename__ = "processing_tasks"
    __table_args__ = (UniqueConstraint("file_id", "stage", name="uq_processing_tasks_file_stage"),)
    task_id = Column(String(64), primary_key=True)
    file_id = Column(String(64), nullable=False, index=True)
    stage = Column(String(32), nullable=False)
    status = Column(String(32), default="PENDING")
    retry = Column(Integer, default=0)
    error_code = Column(String(64), nullable=True)
    error_msg = Column(Text, nullable=True)
    content_version = Column(Integer, nullable=True)
    task_origin = Column(String(16), nullable=False, default="V7")


class DedupMember(Base):
    __tablename__ = "dedup_members"
    file_id = Column(String(64), primary_key=True)
    source_sha256 = Column(String(64), nullable=False, index=True)
    nbytes = Column(Integer, nullable=False)
    group_id = Column(String(64), nullable=False, index=True)
    master_id = Column(String(64), nullable=False)
    is_exact_dup = Column(Integer, default=0)


class ReviewRecord(Base):
    __tablename__ = "review_records"
    id = Column(Integer, primary_key=True, autoincrement=True)
    result_id = Column(Integer, nullable=False, index=True)
    file_id = Column(String(64), nullable=True)
    reviewer = Column(String(128), nullable=False)
    decision = Column(String(32), nullable=False)
    from_version = Column(Integer, nullable=False)
    to_version = Column(Integer, nullable=False)
    level1 = Column(String(64), nullable=True)
    level2 = Column(String(128), nullable=True)
    reason = Column(String(32), nullable=True)
    note = Column(Text, nullable=True)


def make_engine(url: str):
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=connect_args, future=True)


def make_session(engine):
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)


def init_db(engine):
    Base.metadata.create_all(engine)
