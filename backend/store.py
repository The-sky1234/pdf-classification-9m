from __future__ import annotations
import re
import uuid
from datetime import datetime
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from backend.models import (
    ArchiveDecision, ArchiveExecution, ClassificationResult, DocTask, FileContentVersion,
    FileRegistry, ProcessingTask, ReviewRecord, file_hash_of,
)
from classifier.taxonomy_v3 import L2, target_dir

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


def _pair_status(l1, l2) -> str:
    names = {n for vs in L2.values() for n in vs}
    if l2 not in names:
        return "legacy"
    if l2 in L2.get(l1 or "", []):
        return "valid"
    return "illegal"


def _byte_sha(payload: dict) -> str | None:
    sha = payload.get("source_sha256")
    if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha):
        return sha
    return None


def save_result(db: Session, payload: dict) -> ClassificationResult:
    rt = payload.get("result_type")
    if rt not in VALID_RESULT_TYPES:
        raise ValueError(f"invalid result_type: {rt}")
    l1, l2 = payload.get("level1"), payload.get("level2")
    if rt == "CLASSIFIED" and not (l1 and l2):
        raise ValueError("CLASSIFIED requires level1+level2")
    if rt != "CLASSIFIED" and (l1 or l2):
        raise ValueError(f"{rt} must not carry categories")
    pair = _pair_status(l1, l2) if rt == "CLASSIFIED" else "legacy"
    if pair == "illegal":
        raise ValueError("illegal parent")
    fid = payload.get("file_id")
    ver = payload.get("version") or payload.get("decision_version")
    if fid and ver is None:
        ver = 1
    if fid and ver is not None:
        existing = db.scalar(select(ClassificationResult).where(
            ClassificationResult.file_id == fid, ClassificationResult.version == int(ver)))
        if existing:
            return existing
    content_version = payload.get("content_version")
    if not isinstance(content_version, int) or content_version < 1:
        content_version = None
    r = ClassificationResult(**{k: payload.get(k) for k in (
        "task_id", "file_path", "file_hash", "result_type", "relevance_status",
        "evidence_mode", "level1", "level2", "candidate_scores", "decision_source",
        "classification_basis", "model_version", "taxonomy_version", "prompt_version",
        "retry_count", "error")} )
    r.file_id = fid
    r.version = int(ver) if ver is not None else None
    r.content_version = content_version
    db.add(r)
    db.flush()
    t = db.get(DocTask, payload.get("task_id")) if payload.get("task_id") else None
    if t:
        t.stage, t.status = "SAVE", ("DONE" if rt in TERMINAL else "RUNNING")
    if fid and rt == "CLASSIFIED" and pair == "valid":
        status = "APPROVED" if payload.get("calibrated") is True else "NEED_REVIEW"
        atype = "BUSINESS" if status == "APPROVED" else "PENDING"
        reason = None
        level1, level2 = (l1, l2) if atype == "BUSINESS" else (None, None)
        _add_decision(db, r, fid, int(ver or 1), atype, status, level1, level2, reason,
                      content_version, _byte_sha(payload))
    elif fid and rt in ("MANUAL_REVIEW", "UNRESOLVED"):
        _add_decision(db, r, fid, int(ver or 1), "PENDING", "NEED_REVIEW", None, None,
                      rt, content_version, _byte_sha(payload))
    db.commit()
    db.refresh(r)
    return r


def _add_decision(db, result, fid, version, archive_type, decision_status, l1, l2, reason,
                  content_version, source_sha256):
    rel_reason = reason if archive_type == "LOW_VALUE" else None
    rel = target_dir(archive_type, None, l1, l2, rel_reason)
    db.add(ArchiveDecision(
        file_id=fid, version=version, archive_type=archive_type,
        decision_status=decision_status, level1=l1, level2=l2,
        reason=reason if archive_type == "LOW_VALUE" else None,
        target_dir=rel, content_version=content_version, source_sha256=source_sha256,
        approved=1 if decision_status == "APPROVED" else 0, result_id=result.id))


def query_results(db: Session, result_type: str | None = None, level1: str | None = None,
                  review_pending: bool = False, page: int = 1, size: int = 20,
                  archive_status: str | None = None):
    q = select(ClassificationResult).order_by(ClassificationResult.id.desc())
    if result_type:
        q = q.where(ClassificationResult.result_type == result_type)
    if level1:
        q = q.where(ClassificationResult.level1 == level1)
    if archive_status:
        ids = db.scalars(select(ArchiveExecution.file_id).where(
            ArchiveExecution.status == archive_status)).all()
        q = q.where(ClassificationResult.file_id.in_(ids or ["__none__"]))
    if review_pending:
        q = q.where(ClassificationResult.result_type.in_(("MANUAL_REVIEW", "UNRESOLVED")),
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


def _max_decision_version(db: Session, file_id: str | None) -> int:
    if not file_id:
        return 0
    n = db.scalar(select(func.max(ArchiveDecision.version)).where(ArchiveDecision.file_id == file_id))
    return int(n or 0)


def apply_review_decision(db: Session, rid: int, patch: dict) -> dict:
    r = db.get(ClassificationResult, rid)
    if not r or r.result_type not in ("MANUAL_REVIEW", "UNRESOLVED"):
        raise LookupError("review item not found or not in MANUAL_REVIEW")
    if not r.file_id:
        r.file_id = f"result-{r.id}"
    decision = patch.get("decision")
    current = _max_decision_version(db, r.file_id)
    expected = patch.get("expected_version")
    if expected is not None and int(expected) != current:
        raise RuntimeError("version conflict")
    l1, l2, reason = patch.get("level1"), patch.get("level2"), patch.get("reason")
    archive_type = None
    decision_status = "APPROVED"
    if decision == "PATENT":
        archive_type = "PATENT"
    elif decision == "LOW_VALUE":
        if reason not in ("LV01", "LV02", "LV03", "LV04", "LV05"):
            raise ValueError("LOW_VALUE requires LV01-LV05")
        archive_type = "LOW_VALUE"
    elif decision == "BUSINESS":
        if _pair_status(l1, l2) != "valid":
            raise ValueError("illegal parent")
        archive_type = "BUSINESS"
    elif decision == "RETURN":
        archive_type = None
    else:
        raise ValueError("decision must be PATENT, LOW_VALUE, BUSINESS, or RETURN")
    to_version = current + 1
    db.add(ReviewRecord(result_id=r.id, file_id=r.file_id, reviewer=patch["reviewer"],
                        decision=decision, from_version=current, to_version=to_version,
                        level1=l1, level2=l2, reason=reason, note=patch.get("note") or ""))
    r.reviewed_by = patch["reviewer"]
    r.reviewed_level1 = l1
    r.reviewed_level2 = l2
    queued = None
    if archive_type:
        rel_reason = reason if archive_type == "LOW_VALUE" else None
        rel = target_dir(archive_type, None, l1 if archive_type == "BUSINESS" else None,
                         l2 if archive_type == "BUSINESS" else None, rel_reason)
        row = ArchiveDecision(
            file_id=r.file_id, version=to_version, archive_type=archive_type,
            decision_status=decision_status, level1=l1 if archive_type == "BUSINESS" else None,
            level2=l2 if archive_type == "BUSINESS" else None,
            reason=reason if archive_type == "LOW_VALUE" else None,
            target_dir=rel, content_version=r.content_version, approved=1, result_id=r.id)
        db.add(row)
        queued = {"file_id": row.file_id, "version": to_version, "content_version": r.content_version}
    db.commit()
    db.refresh(r)
    return {"id": r.id, "reviewed_by": r.reviewed_by, "to_version": to_version, "queue": queued}


def task_view(db: Session, task_id: str) -> dict | None:
    doc = db.get(DocTask, task_id)
    proc = db.get(ProcessingTask, task_id)
    if doc is None and proc is None:
        return None
    file_id = proc.file_id if proc else None
    content_version = proc.content_version if proc else None
    reg = db.get(FileRegistry, file_id) if file_id else None
    if reg is not None:
        content_version = reg.content_version
    archive_status = None
    if file_id:
        ex = _latest_execution(db, file_id)
        archive_status = ex.status if ex else None
    src = doc or proc
    return {"task_id": task_id,
            "file_path": doc.file_path if doc else reg.source_path if reg else "",
            "stage": doc.stage if doc else proc.stage,
            "status": doc.status if doc else proc.status,
            "retry_count": doc.retry_count if doc else proc.retry,
            "error_code": doc.error_code if doc else proc.error_code,
            "error_msg": doc.error_msg if doc else proc.error_msg,
            "file_id": file_id,
            "content_version": content_version,
            "archive_status": archive_status}


def _latest_decision(db: Session, file_id: str) -> ArchiveDecision | None:
    return db.scalar(select(ArchiveDecision).where(ArchiveDecision.file_id == file_id)
                     .order_by(ArchiveDecision.version.desc()))


def _latest_execution(db: Session, file_id: str) -> ArchiveExecution | None:
    return db.scalar(select(ArchiveExecution).where(ArchiveExecution.file_id == file_id)
                     .order_by(ArchiveExecution.id.desc()))


def execution_for(db: Session, decision: ArchiveDecision) -> ArchiveExecution | None:
    return db.scalar(select(ArchiveExecution).where(ArchiveExecution.decision_id == decision.id)
                     .order_by(ArchiveExecution.id.desc()))


def archive_view(db: Session, file_id: str) -> dict | None:
    decision = _latest_decision(db, file_id)
    if decision is None:
        return None
    ex = execution_for(db, decision)
    status = ex.status if ex else "QUEUED"
    bytes_ok = bool(ex.bytes_ok) if ex else False
    sha_ok = bool(ex.sha_ok) if ex else False
    src_ok = bool(ex.src_unchanged) if ex else False
    business = (decision.archive_type != "PENDING" and status == "COMPLETED"
                and bytes_ok and sha_ok and src_ok)
    return {"file_id": file_id, "version": decision.version,
            "content_version": decision.content_version,
            "archive_type": decision.archive_type, "level1": decision.level1,
            "level2": decision.level2, "reason": decision.reason,
            "target_dir": decision.target_dir,
            "final_path": ex.final_path if ex else None,
            "status": status, "bytes_ok": bytes_ok, "sha_ok": sha_ok,
            "src_unchanged": src_ok,
            "completed_at": ex.completed_at.isoformat() if ex and ex.completed_at else None,
            "counts_as_business_archive": business,
            "checksum_verified": bool(ex.checksum_verified) if ex else False}


def result_extra(db: Session, result: ClassificationResult) -> dict:
    view = archive_view(db, result.file_id) if result.file_id else None
    return {"file_id": result.file_id,
            "decision_version": result.version,
            "content_version": result.content_version,
            "target_path": (view or {}).get("final_path") or (view or {}).get("target_dir"),
            "archive_status": None if view is None else view["status"],
            "checksum_verified": bool(view and view["checksum_verified"]
                                      and view["bytes_ok"] and view["sha_ok"] and view["src_unchanged"])}


def list_item_extra(db: Session, result: ClassificationResult) -> dict:
    extra = result_extra(db, result)
    return {"target_path": extra["target_path"], "archive_status": extra["archive_status"]}


def _mtime(value) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.utcfromtimestamp(float(value))


def note_content(db: Session, file_id: str, source_sha256: str, nbytes: int, mtime: float) -> tuple[int, bool]:
    """首次确认哈希写成版本 1，不把非 SCAN 打回 PENDING。之后升版才重开非 SCAN 任务。"""
    if not re.fullmatch(r"[0-9a-f]{64}", source_sha256 or ""):
        raise ValueError("sha incomplete")
    reg = db.get(FileRegistry, file_id)
    if reg is None:
        raise LookupError("file not registered")
    if reg.source_sha256 == source_sha256 and reg.content_version:
        return reg.content_version, False
    first = reg.content_version is None
    version = 1 if first else int(reg.content_version) + 1
    reg.source_sha256 = source_sha256
    reg.content_version = version
    reg.nbytes = nbytes
    reg.mtime = _mtime(mtime)
    db.add(FileContentVersion(file_id=file_id, content_version=version, source_sha256=source_sha256,
                              nbytes=nbytes, mtime=_mtime(mtime), observed_at=datetime.utcnow(),
                              batch_id=reg.batch_id))
    tasks = db.scalars(select(ProcessingTask).where(ProcessingTask.file_id == file_id)).all()
    for task in tasks:
        task.content_version = version
        if not first and task.stage != "SCAN":
            task.status = "PENDING"
            task.retry = 0
            task.error_code = None
            task.error_msg = None
    db.commit()
    return version, not first


def pending_archive_message(db: Session, file_id: str | None) -> dict | None:
    """最新判决还没有 COMPLETED 执行时，返回可以投入 archive_tasks 的消息。"""
    if not file_id:
        return None
    decision = _latest_decision(db, file_id)
    if decision is None:
        return None
    done = db.scalar(select(ArchiveExecution).where(
        ArchiveExecution.decision_id == decision.id, ArchiveExecution.status == "COMPLETED"))
    if done:
        return None
    return {"file_id": decision.file_id, "version": decision.version,
            "content_version": decision.content_version}


def master_is_patent(db: Session, master_id: str) -> bool:
    row = db.scalar(select(ArchiveDecision).where(
        ArchiveDecision.file_id == master_id, ArchiveDecision.archive_type == "PATENT",
        ArchiveDecision.decision_status == "APPROVED"))
    return row is not None


def write_decision(db: Session, decision: dict, result_id: int | None = None) -> ArchiveDecision:
    version = int(decision.get("version") or 1)
    fid = decision["file_id"]
    existing = db.scalar(select(ArchiveDecision).where(
        ArchiveDecision.file_id == fid, ArchiveDecision.version == version))
    if existing:
        return existing
    row = ArchiveDecision(
        file_id=fid, version=version, archive_type=decision["archive_type"],
        decision_status=decision["decision_status"], level1=decision.get("level1"),
        level2=decision.get("level2"),
        reason=decision.get("reason") if decision["archive_type"] == "LOW_VALUE" else None,
        target_dir=decision["target_dir"], content_version=decision.get("content_version"),
        source_sha256=decision.get("source_sha256"), approved=1 if decision["decision_status"] == "APPROVED" else 0,
        result_id=result_id)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def record_execution(db: Session, decision_id: int, file_id: str, checks: dict) -> ArchiveExecution:
    from archive.service import commit_completed
    decision = db.get(ArchiveDecision, decision_id)
    if not commit_completed(checks):
        row = ArchiveExecution(decision_id=decision_id, file_id=file_id, status="FAILED",
                               content_version=decision.content_version if decision else None,
                               bytes_ok=0, sha_ok=0, src_unchanged=0, checksum_verified=0,
                               final_path=checks.get("final_path"))
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
    same = db.scalar(select(ArchiveExecution).where(
        ArchiveExecution.decision_id == decision_id, ArchiveExecution.status == "COMPLETED"))
    if same:
        return same
    prev = db.scalar(select(ArchiveExecution).where(
        ArchiveExecution.file_id == file_id, ArchiveExecution.status == "COMPLETED"))
    if prev and prev.decision_id != decision_id:
        prev.status = "SUPERSEDED"
    row = ArchiveExecution(
        decision_id=decision_id, file_id=file_id, status="COMPLETED",
        content_version=decision.content_version if decision else checks.get("content_version"),
        final_path=checks["final_path"], target_sha256=checks.get("sha"),
        source_sha256=checks.get("sha"), target_bytes=checks.get("nbytes"),
        bytes_ok=1, sha_ok=1, src_unchanged=1, checksum_verified=1,
        completed_at=datetime.utcnow())
    db.add(row)
    db.commit()
    db.refresh(row)
    return row
