from __future__ import annotations
import uuid
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from backend.models import DocTask, ClassificationResult, file_hash_of

VALID_RESULT_TYPES = {"CLASSIFIED", "IRRELEVANT", "MANUAL_REVIEW", "FAILED", "UNRESOLVED"}
TERMINAL = {"CLASSIFIED", "IRRELEVANT", "MANUAL_REVIEW", "FAILED", "UNRESOLVED"}


def create_task(db: Session, file_path: str) -> DocTask:
    fh = file_hash_of(file_path)
    tid = uuid.uuid5(uuid.NAMESPACE_URL, f"{file_path}:{fh}").hex
    t = db.get(DocTask, tid)
    if t:
        return t
    t = DocTask(task_id=tid, file_path=file_path, file_hash=fh, stage="SCAN", status="PENDING")
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def get_task(db: Session, task_id: str) -> DocTask | None:
    return db.get(DocTask, task_id)


def update_task_stage(db: Session, task_id: str, stage: str, status: str = "RUNNING",
                      error_code: str | None = None, error_msg: str | None = None) -> DocTask | None:
    t = db.get(DocTask, task_id)
    if not t:
        return None
    t.stage, t.status = stage, status
    if error_code is not None:
        t.error_code, t.error_msg = error_code, error_msg
    db.commit()
    db.refresh(t)
    return t


def save_result(db: Session, payload: dict) -> ClassificationResult:
    rt = payload.get("result_type")
    if rt not in VALID_RESULT_TYPES:
        raise ValueError(f"invalid result_type: {rt}")
    l1, l2 = payload.get("level1"), payload.get("level2")
    if rt == "CLASSIFIED" and not (l1 and l2):
        raise ValueError("CLASSIFIED requires level1+level2")
    if rt != "CLASSIFIED" and (l1 or l2):
        raise ValueError(f"{rt} must not carry categories")
    r = ClassificationResult(**{k: payload.get(k) for k in (
        "task_id", "file_path", "file_hash", "result_type", "relevance_status",
        "evidence_mode", "level1", "level2", "candidate_scores", "decision_source",
        "classification_basis", "model_version", "taxonomy_version", "prompt_version",
        "retry_count", "error")} )
    db.add(r)
    db.commit()
    db.refresh(r)
    t = db.get(DocTask, payload.get("task_id")) if payload.get("task_id") else None
    if t:
        t.stage, t.status = "SAVE", ("DONE" if rt in TERMINAL else "RUNNING")
        db.commit()
    return r


def query_results(db: Session, result_type: str | None = None, level1: str | None = None,
                  review_pending: bool = False, page: int = 1, size: int = 20):
    q = select(ClassificationResult).order_by(ClassificationResult.id.desc())
    if result_type:
        q = q.where(ClassificationResult.result_type == result_type)
    if level1:
        q = q.where(ClassificationResult.level1 == level1)
    if review_pending:
        q = q.where(ClassificationResult.result_type == "MANUAL_REVIEW",
                    ClassificationResult.reviewed_by.is_(None))
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    rows = db.scalars(q.offset((page - 1) * size).limit(size)).all()
    return total, rows


def apply_review(db: Session, rid: int, patch: dict) -> ClassificationResult | None:
    r = db.get(ClassificationResult, rid)
    if not r or r.result_type != "MANUAL_REVIEW":
        return None
    r.reviewed_by = patch["reviewer"]
    r.reviewed_level1 = patch.get("level1")
    r.reviewed_level2 = patch.get("level2")
    db.commit()
    db.refresh(r)
    return r
