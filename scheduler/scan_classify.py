from __future__ import annotations
import os
from sqlalchemy import select
from sqlalchemy.orm import Session
from backend import store
from backend.models import ArchiveDecision, ArchiveExecution, FileRegistry
from classifier.config import Thresholds
from classifier.v3_label import make_runtime_classifier
from registry.register import scan_register
from scheduler.phase1_worker import _file_sha, handle_archive_task, handle_document_task


def run(db: Session, root: str, archive_root: str, *, limit: int = 200,
        batch_id: int | None = None, classify_fn=None, calibrated: bool | None = None) -> dict:
    """登记目录，取前五页文字，用模型判标签，再复制到对应目录。

    有文字的页直接抽取，图片页才 OCR。模型给出合法标签就写入业务目录。
    单份文件损坏或分类失败只记入待复核，不中断其余文件。同一批次可续跑。
    """
    if _archive_inside_source(root, archive_root):
        raise ValueError("archive is inside source root")
    if classify_fn is None:
        if calibrated is None:
            calibrated = os.environ.get("THRESHOLDS_CALIBRATED", "0") == "1"
        classify_fn = make_runtime_classifier(Thresholds(calibrated=bool(calibrated)))
    os.makedirs(archive_root, exist_ok=True)
    counts: dict[str, int] = {}
    seen_batch = batch_id
    while True:
        scanned = scan_register(db, root, limit, seen_batch, archive_root)
        seen_batch = scanned["batch_id"]
        for item in scanned["items"]:
            outcome = _one(db, item, archive_root, classify_fn)
            counts[outcome] = counts.get(outcome, 0) + 1
        if scanned["accepted"] < limit:
            break
    return {"batch_id": seen_batch, "counts": counts}


def _one(db: Session, item: dict, archive_root: str, classify_fn) -> str:
    finished = _finished_copy(db, item["file_id"])
    if finished == "SKIP":
        return "SKIP"
    if finished == "ARCHIVE":
        archived = handle_archive_task(db, _queue_message(db, item["file_id"]), archive_root)
        return archived["status"]
    try:
        decided = handle_document_task(db, item, classify_fn=classify_fn)
    except OSError:
        return "MISSING"
    except Exception as exc:
        db.rollback()
        try:
            decided = handle_document_task(db, item, classify_fn=lambda _parse: {
                "result_type": "MANUAL_REVIEW", "reason": f"classify_failed: {exc}"[:80]})
        except Exception:
            return "ERROR"
    action = decided.get("action")
    if action == "RETRY":
        return "RETRY"
    if action != "DECIDE":
        return action or "DROP"
    archived = handle_archive_task(db, decided["queue"], archive_root)
    view = store.archive_view(db, item["file_id"]) or {}
    if view.get("archive_type") == "PENDING":
        return "REVIEW"
    if view.get("archive_type") == "PATENT":
        return "PATENT"
    if view.get("archive_type") == "LOW_VALUE":
        return "LOW_VALUE"
    if view.get("archive_type") == "BUSINESS" and archived["status"] == "COMPLETED":
        return "BUSINESS"
    return archived["status"]


def _archive_inside_source(root: str, archive_root: str) -> bool:
    try:
        src = os.path.realpath(root)
        dst = os.path.realpath(archive_root)
        return os.path.commonpath([src, dst]) == src
    except ValueError:
        return False


def _finished_copy(db: Session, file_id: str) -> str | None:
    reg = db.get(FileRegistry, file_id)
    if reg is None or not reg.source_sha256 or not reg.content_version:
        return None
    try:
        sha, nbytes = _file_sha(reg.source_path)
    except OSError:
        return None
    if sha != reg.source_sha256 or nbytes != reg.nbytes:
        return None
    decision = db.scalar(select(ArchiveDecision).where(
        ArchiveDecision.file_id == file_id,
        ArchiveDecision.content_version == reg.content_version).order_by(ArchiveDecision.version.desc()))
    if decision is None:
        return None
    done = db.scalar(select(ArchiveExecution).where(
        ArchiveExecution.decision_id == decision.id, ArchiveExecution.status == "COMPLETED"))
    return "SKIP" if done else "ARCHIVE"


def _queue_message(db: Session, file_id: str) -> dict:
    reg = db.get(FileRegistry, file_id)
    decision = db.scalar(select(ArchiveDecision).where(
        ArchiveDecision.file_id == file_id).order_by(ArchiveDecision.version.desc()))
    return {"file_id": file_id, "version": decision.version,
            "content_version": reg.content_version if reg else None}
