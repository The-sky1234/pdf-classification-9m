from __future__ import annotations
import os
from sqlalchemy import select
from sqlalchemy.orm import Session
from backend.models import (
    ArchiveDecision, ArchiveExecution, DocTask, FileRegistry, ProcessingTask,
)
from registry.ids import uuid7
from scheduler.phase1_worker import handle_archive_task, handle_document_task

MAX_RETRY = 3
DECIDE_STAGE = "DECIDE"


def default_classifier():
    from classifier.config import Thresholds
    from classifier.v3_label import make_v3_classifier
    calibrated = os.environ.get("THRESHOLDS_CALIBRATED", "0") == "1"
    return make_v3_classifier(Thresholds(calibrated=calibrated))


def redrive(db: Session, pdf_queue, archive_queue) -> dict:
    """队列丢失后，把仍未做完的登记任务和归档决策重新放进去。

    已经躺在队列里的不重复放。处理中途进程消失时，先放开 inflight 再投递。
    """
    pdf_n = 0
    tasks = db.scalars(select(ProcessingTask).where(ProcessingTask.status == "PENDING")).all()
    for task in tasks:
        if task.stage not in ("SCAN", DECIDE_STAGE):
            continue
        reg = db.get(FileRegistry, task.file_id)
        if reg is None:
            continue
        if task.stage == "SCAN" and _decide_caught_up(db, reg):
            continue
        if pdf_queue.in_queue(task.task_id):
            continue
        pdf_queue.release(task.task_id)
        msg = {"v": 1, "task_id": task.task_id, "file_id": task.file_id,
               "content_version": reg.content_version, "source_path": reg.source_path}
        if pdf_queue.enqueue(task.task_id, msg):
            pdf_n += 1
    archive_n = 0
    latest: dict[str, ArchiveDecision] = {}
    for row in db.scalars(select(ArchiveDecision)).all():
        cur = latest.get(row.file_id)
        if cur is None or row.version > cur.version:
            latest[row.file_id] = row
    for row in latest.values():
        done = db.scalar(select(ArchiveExecution).where(
            ArchiveExecution.decision_id == row.id, ArchiveExecution.status == "COMPLETED"))
        if done:
            continue
        key = f"{row.file_id}:{row.version}"
        if archive_queue.in_queue(key):
            continue
        archive_queue.release(key)
        payload = {"file_id": row.file_id, "version": row.version,
                   "content_version": row.content_version}
        if archive_queue.enqueue(key, payload):
            archive_n += 1
    return {"pdf": pdf_n, "archive": archive_n}


def drain_once(db: Session, pdf_queue, archive_queue, archive_root: str, *,
               classify_fn=None, consume: tuple[str, ...] = ("decide", "archive")) -> dict:
    """取一条消息。decide 写判决并投入归档队列，archive 按判决复制。"""
    if "decide" in consume:
        item = pdf_queue.dequeue()
        if item:
            return _consume_pdf(db, pdf_queue, archive_queue, item, classify_fn)
    if "archive" in consume:
        item = archive_queue.dequeue()
        if item:
            return _consume_archive(db, archive_queue, archive_root, item)
    return {"kind": "idle"}


def drain_all(db: Session, pdf_queue, archive_queue, archive_root: str, *,
              classify_fn=None, consume: tuple[str, ...] = ("decide", "archive"),
              limit: int = 10000) -> list[dict]:
    steps = []
    while len(steps) < limit:
        step = drain_once(db, pdf_queue, archive_queue, archive_root,
                          classify_fn=classify_fn, consume=consume)
        if step["kind"] == "idle":
            break
        steps.append(step)
    return steps


def serve(role: str, stop) -> int:
    """常驻消费。decide 读 pdf_tasks，archive 读 archive_tasks。启动时先补投未完成的行。"""
    import time
    from backend.models import init_db, make_engine, make_session
    from scheduler.queue import open_queue
    if role not in ("decide", "archive"):
        raise SystemExit(f"unknown phase1 role {role}")
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        raise SystemExit("missing required env DATABASE_URL")
    archive_root = os.environ.get("ARCHIVE_ROOT", "")
    if role == "archive" and not archive_root:
        raise SystemExit("missing required env ARCHIVE_ROOT")
    pdf_queue = open_queue("pdf_tasks")
    archive_queue = open_queue("archive_tasks")
    engine = make_engine(db_url)
    init_db(engine)
    sessions = make_session(engine)
    db = sessions()
    try:
        redrive(db, pdf_queue, archive_queue)
    finally:
        db.close()
    classify_fn = default_classifier()
    consume = (role,)
    idle = 0
    while not stop():
        db = sessions()
        try:
            step = drain_once(db, pdf_queue, archive_queue, archive_root,
                              classify_fn=classify_fn, consume=consume)
        finally:
            db.close()
        if step["kind"] == "idle":
            idle += 1
            time.sleep(min(5.0, 0.1 * idle))
            continue
        idle = 0
    return 0


def _consume_pdf(db, pdf_queue, archive_queue, item, classify_fn) -> dict:
    task_id, payload = item
    try:
        outcome = handle_document_task(db, payload, classify_fn=classify_fn or default_classifier())
        state = _remember(db, payload, outcome)
    except Exception as exc:
        _mark_failed(db, payload, str(exc))
        pdf_queue.release(task_id)
        pdf_queue.to_dead({"task_id": task_id, "error": str(exc)})
        return {"kind": "pdf", "action": "DEAD", "error": str(exc)}
    pdf_queue.release(task_id)
    action = outcome.get("action")
    if action == "RETRY" and state.get("retry", 0) <= MAX_RETRY:
        reg = db.get(FileRegistry, payload.get("file_id"))
        if reg is not None:
            payload = dict(payload, content_version=reg.content_version)
        pdf_queue.enqueue(task_id, payload)
    elif action == "RETRY":
        pdf_queue.to_dead({"task_id": task_id, "error": outcome.get("error_code") or "RETRY"})
    elif action == "DECIDE" and outcome.get("queue"):
        queued = outcome["queue"]
        archive_queue.enqueue(f"{queued['file_id']}:{queued['version']}", queued)
    return {"kind": "pdf", "action": action, "result_id": outcome.get("result_id")}


def _consume_archive(db, archive_queue, archive_root, item) -> dict:
    key, payload = item
    try:
        outcome = handle_archive_task(db, payload, archive_root)
    except Exception as exc:
        archive_queue.release(key)
        archive_queue.to_dead({"task_id": key, "error": str(exc)})
        return {"kind": "archive", "action": "DEAD", "error": str(exc)}
    archive_queue.release(key)
    status = outcome.get("status") or outcome.get("action")
    retry = int(payload.get("retry") or 0)
    if status == "FAILED" and retry < MAX_RETRY:
        again = dict(payload, retry=retry + 1)
        archive_queue.enqueue(key, again)
    elif status == "FAILED":
        archive_queue.to_dead({"task_id": key, "error": outcome.get("reason") or "FAILED"})
    return {"kind": "archive", "action": outcome.get("action"), "status": outcome.get("status")}


def _remember(db: Session, message: dict, outcome: dict) -> dict:
    version = (outcome.get("queue") or {}).get("content_version")
    if db.get(FileRegistry, message.get("file_id")) is None:
        return {"retry": 0}
    proc = db.get(ProcessingTask, message.get("task_id"))
    decide = _ensure_decide(db, message["file_id"], version)
    action = outcome.get("action")
    retry = int(proc.retry or 0) if proc is not None else 0
    if action == "DECIDE":
        if proc is not None:
            proc.status = "DONE"
            if version is not None:
                proc.content_version = version
        decide.status = "DONE"
        decide.content_version = version
        doc = db.get(DocTask, message.get("task_id"))
        if doc is not None:
            doc.status = "DONE"
            doc.stage = "SAVE"
    elif action == "RETRY":
        retry += 1
        if proc is not None:
            proc.retry = retry
            proc.status = "FAILED" if retry > MAX_RETRY else "PENDING"
            proc.error_code = outcome.get("error_code")
        decide.retry = retry
        decide.status = "FAILED" if retry > MAX_RETRY else "PENDING"
        decide.error_code = outcome.get("error_code")
    db.commit()
    return {"retry": retry}


def _mark_failed(db: Session, message: dict, error: str) -> None:
    proc = db.get(ProcessingTask, message.get("task_id"))
    if proc is not None:
        proc.status = "FAILED"
        proc.error_msg = error[:500]
        db.commit()


def _ensure_decide(db: Session, file_id: str, content_version) -> ProcessingTask:
    row = db.scalar(select(ProcessingTask).where(
        ProcessingTask.file_id == file_id, ProcessingTask.stage == DECIDE_STAGE))
    if row is not None:
        return row
    task_id = uuid7()
    while task_id == file_id:
        task_id = uuid7()
    row = ProcessingTask(task_id=task_id, file_id=file_id, stage=DECIDE_STAGE, status="PENDING",
                         retry=0, task_origin="V7", content_version=content_version)
    db.add(row)
    db.flush()
    return row


def _decide_caught_up(db: Session, reg: FileRegistry) -> bool:
    decide = db.scalar(select(ProcessingTask).where(
        ProcessingTask.file_id == reg.file_id, ProcessingTask.stage == DECIDE_STAGE))
    return bool(decide and decide.status == "DONE" and decide.content_version == reg.content_version)
