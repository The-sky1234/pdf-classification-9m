from __future__ import annotations
import os
from sqlalchemy import select
from sqlalchemy.orm import Session
from backend.models import (
    DocTask, FileRegistry, ProcessingBatch, ProcessingTask, file_hash_of,
)
from registry.ids import uuid7
from registry.kinds import is_document


def _inside_archive(root: str, archive_root: str | None) -> bool:
    if not archive_root:
        return False
    try:
        return os.path.commonpath([os.path.realpath(root), os.path.realpath(archive_root)]) == os.path.realpath(archive_root)
    except ValueError:
        return False


def _document_paths(root: str) -> list[str]:
    found: list[str] = []
    for dp, dirnames, fns in os.walk(root):
        dirnames.sort()
        for fn in fns:
            if is_document(fn):
                found.append(os.path.normpath(os.path.abspath(os.path.join(dp, fn))))
    found.sort()
    return found


def scan_register(db: Session, root: str, limit: int = 1000, batch_id: int | None = None,
                  archive_root: str | None = None) -> dict:
    """登记只读目录。同一规范路径返回原来的 file_id 和 SCAN task_id。"""
    if _inside_archive(root, archive_root):
        raise ValueError("root is inside ARCHIVE_ROOT")
    if batch_id is None:
        batch = ProcessingBatch(batch_no=uuid7(), source_roots=os.path.abspath(root),
                                scan_status="RUNNING")
        db.add(batch)
        db.commit()
        db.refresh(batch)
    else:
        batch = db.get(ProcessingBatch, batch_id)
        if batch is None:
            raise ValueError("batch not found")
    cursor = batch.cursor_path or ""
    items: list[dict] = []
    blocked = False
    for path in _document_paths(root):
        if cursor and path <= cursor:
            continue
        if len(items) >= limit:
            break
        try:
            st = os.lstat(path)
        except OSError as e:
            batch.skipped_count = (batch.skipped_count or 0) + 1
            batch.last_error = f"{path}: {e}"
            blocked = True
            db.commit()
            continue
        if not stat_ok_file(st):
            continue
        item = _upsert_file(db, batch, path, st)
        items.append(item)
        if not blocked:
            batch.cursor_path = path
        db.commit()
    if len(items) < limit and not blocked:
        batch.scan_status = "COMPLETED"
        db.commit()
    return {"accepted": len(items), "batch_id": batch.id,
            "task_ids": [it["task_id"] for it in items], "items": items}


def stat_ok_file(st) -> bool:
    return stat_is_reg(st.st_mode)


def stat_is_reg(mode: int) -> bool:
    import stat
    return stat.S_ISREG(mode)


def _upsert_file(db: Session, batch: ProcessingBatch, path: str, st) -> dict:
    row = db.scalar(select(FileRegistry).where(FileRegistry.source_path == path))
    if row is None:
        fid = uuid7()
        tid = uuid7()
        while tid == fid:
            tid = uuid7()
        row = FileRegistry(file_id=fid, source_path=path, nbytes=st.st_size,
                           mtime=st.st_mtime, batch_id=batch.id, content_version=None,
                           id_origin="V7")
        task = ProcessingTask(task_id=tid, file_id=fid, stage="SCAN", status="PENDING",
                              retry=0, task_origin="V7", content_version=None)
        db.add(row)
        db.add(task)
        db.add(DocTask(task_id=tid, file_path=path, file_hash=file_hash_of(path),
                       stage="SCAN", status="PENDING"))
        batch.files_registered = (batch.files_registered or 0) + 1
        origin = "V7"
        content_version = None
    else:
        fid = row.file_id
        task = db.scalar(select(ProcessingTask).where(
            ProcessingTask.file_id == fid, ProcessingTask.stage == "SCAN"))
        if task is None:
            tid = uuid7()
            while tid == fid:
                tid = uuid7()
            task = ProcessingTask(task_id=tid, file_id=fid, stage="SCAN", status="PENDING",
                                  retry=0, task_origin="V7", content_version=row.content_version)
            db.add(task)
            if db.get(DocTask, tid) is None:
                db.add(DocTask(task_id=tid, file_path=path, file_hash=file_hash_of(path),
                               stage="SCAN", status="PENDING"))
        origin = row.id_origin
        content_version = row.content_version
        tid = task.task_id
    return {"file_id": fid, "task_id": tid, "source_path": path,
            "id_origin": origin, "content_version": content_version}
