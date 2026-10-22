from __future__ import annotations
import hashlib
import os
from sqlalchemy import select
from sqlalchemy.orm import Session
from archive.service import run_copy
from backend.models import ArchiveDecision, FileRegistry
from backend import store
from dedup.exact import exact
from exclusion.flow import decide
from scheduler.versions import message_is_current


def _file_sha(path: str) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


def handle_pdf_task(db: Session, message: dict, classify_fn=None) -> dict:
    """解析并判决。消息里的 content_version 更旧时丢弃，不写决策。"""
    fid = message["file_id"]
    reg = db.get(FileRegistry, fid)
    if reg is None:
        return {"action": "DROP", "reason": "file not registered"}
    if not message_is_current(message.get("content_version"), reg.content_version):
        return {"action": "DROP", "reason": "stale_content_version"}
    sha, nbytes = _file_sha(reg.source_path)
    version, _bumped = store.note_content(db, fid, sha, nbytes, os.stat(reg.source_path).st_mtime)
    db.refresh(reg)
    dup = exact(db, fid, sha, nbytes)
    if dup["is_exact_dup"] and store.master_is_patent(db, dup["master_id"]):
        dup = dict(dup, master_is_patent=True)
    from parser.parser import parse_pdf
    parsed = parse_pdf(reg.source_path).to_dict()
    decision = decide(fid, parsed, dup, classify_fn=classify_fn, content_version=version)
    if decision.get("action") == "RETRY":
        return decision
    decision["version"] = 1
    decision["source_sha256"] = sha
    payload = {"file_path": reg.source_path, "file_id": fid, "task_id": message.get("task_id"),
               "result_type": decision["result_type"], "version": 1, "content_version": version,
               "source_sha256": sha, "taxonomy_version": "v3.0-phase1",
               "classification_basis": {"reason": decision.get("reason"),
                                        "target_dir": decision.get("target_dir")}}
    if decision["result_type"] == "CLASSIFIED":
        payload["level1"] = decision.get("level1")
        payload["level2"] = decision.get("level2")
        payload["calibrated"] = True
    result = store.save_result(db, payload)
    row = store.write_decision(db, decision, result_id=result.id)
    return {"action": "DECIDE", "result_id": result.id, "decision_id": row.id,
            "queue": {"file_id": fid, "version": row.version, "content_version": version}}


def handle_archive_task(db: Session, message: dict, archive_root: str) -> dict:
    fid = message["file_id"]
    reg = db.get(FileRegistry, fid)
    decision = db.scalar(select(ArchiveDecision).where(
        ArchiveDecision.file_id == fid, ArchiveDecision.version == int(message["version"])))
    if reg is None or decision is None:
        return {"action": "DROP", "reason": "missing"}
    if not message_is_current(message.get("content_version"), reg.content_version):
        return {"action": "DROP", "reason": "stale_content_version"}
    checks = run_copy(reg.source_path, archive_root, {
        "archive_type": decision.archive_type, "level1": decision.level1, "level2": decision.level2,
        "reason": decision.reason, "content_version": decision.content_version,
        "target_dir": decision.target_dir}, reg.content_version)
    execution = store.record_execution(db, decision.id, fid, checks)
    return {"action": execution.status, "final_path": execution.final_path, "status": execution.status}
