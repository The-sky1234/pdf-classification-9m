"""登记用的内存数据库。

只模拟 file_registry 的规范路径唯一约束、批次游标和 scan_owner。
不连接 PostgreSQL，不执行迁移。
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

V7_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
LEGACY_RE = re.compile(r"^[0-9a-f]{32}$")


class FileIdCollision(Exception):
    pass


class RootsMismatch(Exception):
    pass


class MissingBatch(Exception):
    pass


class BatchStateError(Exception):
    """批次状态不允许这次转换。不改游标，也不改已登记行。"""


@dataclass
class FileRecord:
    file_id: str
    source_path: str
    source_sha256: str | None
    nbytes: int
    mtime: datetime
    batch_id: int
    content_version: int | None
    id_origin: str
    created_at: datetime

    def to_dict(self) -> dict:
        return {
            "file_id": self.file_id,
            "source_path": self.source_path,
            "source_sha256": self.source_sha256,
            "bytes": self.nbytes,
            "mtime": self.mtime,
            "batch_id": self.batch_id,
            "content_version": self.content_version,
            "id_origin": self.id_origin,
        }


@dataclass
class BatchRecord:
    id: int
    batch_no: str
    source_roots: tuple[str, ...]
    created_at: datetime
    cursor_path: str | None = None
    scan_status: str = "RUNNING"
    files_registered: int = 0
    skipped_count: int = 0
    scan_owner: str | None = None
    last_error: str | None = None
    blocked_path: str | None = None


@dataclass
class AdvanceResult:
    row: FileRecord | None
    inserted: bool
    advanced: bool


class MemoryRegistryDb:
    def __init__(self):
        self._lock = threading.Lock()
        self._by_path: dict[str, FileRecord] = {}
        self._by_id: dict[str, FileRecord] = {}
        self._batches: dict[str, BatchRecord] = {}
        self._batches_by_id: dict[int, BatchRecord] = {}
        self._next_batch_id = 1

    def ensure_batch(self, batch_no: str, roots: tuple[str, ...]) -> BatchRecord:
        if not roots:
            raise ValueError("source_roots is empty")
        with self._lock:
            found = self._batches.get(batch_no)
            if found is not None:
                if found.source_roots != roots:
                    raise RootsMismatch(batch_no)
                return found
            batch = BatchRecord(
                id=self._next_batch_id,
                batch_no=batch_no,
                source_roots=roots,
                created_at=datetime.now(timezone.utc),
            )
            self._next_batch_id += 1
            self._batches[batch_no] = batch
            self._batches_by_id[batch.id] = batch
            return batch

    def claim(self, batch_no: str, worker: str) -> int:
        with self._lock:
            batch = self._batches[batch_no]
            if batch.scan_status in ("RUNNING", "INTERRUPTED") and batch.scan_owner is None:
                batch.scan_owner = worker
                return 1
            return 0

    def release_owner(self, batch_no: str) -> None:
        with self._lock:
            self._batches[batch_no].scan_owner = None

    def reopen_failed(self, batch_no: str) -> BatchRecord:
        """FAILED → INTERRUPTED。调用方必须先释放 scan_owner。

        不修改 cursor_path、files_registered、skipped_count、blocked_path，也不改登记行。
        claim 仍然拒绝 FAILED，避免绕过这次转换直接续跑。
        """
        with self._lock:
            batch = self._batches[batch_no]
            if batch.scan_owner is not None:
                raise BatchStateError("release scan_owner before reopening a FAILED batch")
            if batch.scan_status != "FAILED":
                raise BatchStateError(f"cannot reopen status {batch.scan_status}")
            batch.scan_status = "INTERRUPTED"
            return batch

    def batch(self, batch_no: str) -> BatchRecord:
        with self._lock:
            return self._batches[batch_no]

    def get_by_path(self, source_path: str) -> FileRecord | None:
        with self._lock:
            return self._by_path.get(source_path)

    def get_by_id(self, file_id: str) -> FileRecord | None:
        with self._lock:
            return self._by_id.get(file_id)

    def rows(self) -> list[FileRecord]:
        with self._lock:
            return list(self._by_path.values())

    def seed_legacy(self, *, file_id: str, source_path: str, nbytes: int, mtime: datetime, batch_id: int) -> FileRecord:
        if LEGACY_RE.fullmatch(file_id) is None or V7_RE.fullmatch(file_id):
            raise ValueError("legacy file_id must be 32 lowercase hex digits")
        with self._lock:
            if batch_id not in self._batches_by_id:
                raise MissingBatch(batch_id)
            if source_path in self._by_path or file_id in self._by_id:
                raise ValueError("legacy row already present")
            row = FileRecord(
                file_id=file_id,
                source_path=source_path,
                source_sha256=None,
                nbytes=nbytes,
                mtime=mtime,
                batch_id=batch_id,
                content_version=None,
                id_origin="LEGACY",
                created_at=datetime.now(timezone.utc),
            )
            self._by_path[source_path] = row
            self._by_id[file_id] = row
            return row

    def put_file(self, *, file_id: str, source_path: str, nbytes: int, mtime: datetime, batch_id: int) -> tuple[FileRecord, bool]:
        with self._lock:
            return self._insert_unlocked(file_id, source_path, nbytes, mtime, batch_id)

    def register_and_advance(
        self, *, worker: str, batch_id: int, file_id: str, source_path: str, nbytes: int, mtime: datetime
    ) -> AdvanceResult:
        with self._lock:
            batch = self._require_owner(batch_id, worker)
            row, inserted = self._insert_unlocked(file_id, source_path, nbytes, mtime, batch_id)
            advanced = self._advance_unlocked(batch, source_path, skip=False)
            return AdvanceResult(row, inserted, advanced)

    def skip_and_advance(self, *, worker: str, batch_id: int, lexical: str) -> bool:
        with self._lock:
            batch = self._require_owner(batch_id, worker)
            return self._advance_unlocked(batch, lexical, skip=True)

    def set_blocked_path(self, *, worker: str, batch_id: int, blocked_path: str | None) -> None:
        """记录尚未列举成功的最早目录。传 None 表示这次遍历已经列举成功。不改游标。"""
        with self._lock:
            batch = self._require_owner(batch_id, worker)
            batch.blocked_path = blocked_path

    def mark_failed(self, *, worker: str, batch_id: int, last_error: str) -> None:
        with self._lock:
            batch = self._require_owner(batch_id, worker)
            batch.scan_status = "FAILED"
            batch.last_error = last_error

    def mark_lstat_failed(self, *, worker: str, batch_id: int, lexical: str) -> None:
        self.mark_failed(worker=worker, batch_id=batch_id, last_error=lexical)

    def interrupt(self, *, worker: str, batch_id: int) -> None:
        with self._lock:
            batch = self._require_owner(batch_id, worker)
            batch.scan_status = "INTERRUPTED"

    def complete(self, *, worker: str, batch_id: int) -> None:
        with self._lock:
            batch = self._require_owner(batch_id, worker)
            if batch.blocked_path is not None:
                raise BatchStateError("directory listing error is unresolved")
            batch.scan_status = "COMPLETED"
            batch.scan_owner = None
            batch.last_error = None

    def _require_owner(self, batch_id: int, worker: str) -> BatchRecord:
        batch = self._batches_by_id.get(batch_id)
        if batch is None:
            raise MissingBatch(batch_id)
        if batch.scan_owner != worker:
            raise PermissionError("scan_owner mismatch")
        return batch

    def _insert_unlocked(self, file_id, source_path, nbytes, mtime, batch_id) -> tuple[FileRecord, bool]:
        if V7_RE.fullmatch(file_id) is None:
            raise ValueError("new file_id is not UUIDv7")
        if batch_id not in self._batches_by_id:
            raise MissingBatch(batch_id)
        existing = self._by_path.get(source_path)
        if existing is not None:
            return existing, False
        if file_id in self._by_id:
            raise FileIdCollision(file_id)
        row = FileRecord(
            file_id=file_id,
            source_path=source_path,
            source_sha256=None,
            nbytes=nbytes,
            mtime=mtime,
            batch_id=batch_id,
            content_version=None,
            id_origin="V7",
            created_at=datetime.now(timezone.utc),
        )
        self._by_path[source_path] = row
        self._by_id[file_id] = row
        return row, True

    def _advance_unlocked(self, batch: BatchRecord, canon: str, *, skip: bool) -> bool:
        from registry.paths import path_lt

        if batch.cursor_path is not None and not path_lt(batch.cursor_path, canon):
            return False
        batch.cursor_path = canon
        if skip:
            batch.skipped_count += 1
        else:
            batch.files_registered += 1
        return True

    def roots_json(self, batch_no: str) -> str:
        batch = self.batch(batch_no)
        return json.dumps(list(batch.source_roots), ensure_ascii=False)
