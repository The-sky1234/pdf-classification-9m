"""按规范路径登记 file_id。不生成 task_id，不写 source_sha256，不读写 seen。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from registry.memory import (
    AdvanceResult,
    BatchStateError,
    FileIdCollision,
    FileRecord,
    MemoryRegistryDb,
)
from registry.paths import (
    LstatFailed,
    PathRejected,
    StatFailed,
    canonical_root,
    iter_pdfs,
    lexical_candidate,
    lstat_regular,
    mtime_utc,
    path_lt,
    path_sort_key,
    stat_regular,
)
from registry.uuidv7 import UuidV7Factory

DEFAULT_FACTORY = UuidV7Factory()


class RetryableScanError(Exception):
    pass


class ScanBusy(Exception):
    pass


@dataclass
class RegisterOutcome:
    row: FileRecord
    inserted: bool
    discarded_file_ids: list[str] = field(default_factory=list)


@dataclass
class ScanSummary:
    batch_id: int
    batch_no: str
    scan_status: str
    cursor_path: str | None
    files_registered: int
    skipped_count: int
    last_error: str | None
    outcomes: list[RegisterOutcome]
    stat_errors: list[str] = field(default_factory=list)


def normalize_roots(source_roots: list[str]) -> tuple[str, ...]:
    if not source_roots:
        raise RetryableScanError("source_roots is empty")
    return tuple(canonical_root(root) for root in source_roots)


def register_file(
    db: MemoryRegistryDb,
    *,
    source_path: str,
    nbytes: int,
    mtime,
    batch_id: int,
    factory: UuidV7Factory | None = None,
) -> RegisterOutcome:
    """只插入 file_registry。路径冲突则丢弃新 ID 并读回原行。"""
    factory = factory or DEFAULT_FACTORY
    discarded: list[str] = []
    for _ in range(8):
        file_id = factory.new()
        try:
            row, inserted = db.put_file(
                file_id=file_id,
                source_path=source_path,
                nbytes=nbytes,
                mtime=mtime,
                batch_id=batch_id,
            )
        except FileIdCollision:
            discarded.append(file_id)
            continue
        if not inserted:
            discarded.append(file_id)
        return RegisterOutcome(row, inserted, discarded)
    raise RuntimeError("UUIDv7 primary-key retries exhausted")


def scan_batch(
    db: MemoryRegistryDb,
    *,
    batch_no: str,
    source_roots: list[str],
    worker: str,
    factory: UuidV7Factory | None = None,
    stop_after: int | None = None,
    record_limit: int | None = None,
) -> ScanSummary:
    roots = normalize_roots(source_roots)
    _require_real_dirs(roots)
    db.ensure_batch(batch_no, roots)
    if db.claim(batch_no, worker) != 1:
        raise ScanBusy(batch_no)
    raws: list[str] = []
    listing_errors: list[tuple[str, str]] = []
    for root in roots:
        found, errors = iter_pdfs(root)
        raws.extend(found)
        listing_errors.extend(errors)
    return _process(
        db,
        batch_no,
        roots,
        worker,
        raws,
        factory or DEFAULT_FACTORY,
        stop_after,
        listing_errors,
        walked=True,
        record_limit=record_limit,
    )


def reregister(
    db: MemoryRegistryDb,
    *,
    batch_no: str,
    source_roots: list[str],
    worker: str,
    raw_path: str,
    factory: UuidV7Factory | None = None,
) -> RegisterOutcome:
    """重复投递一条已经扫过的路径。不认领批次，不读取 seen。

    路径仍严格大于游标时拒绝，避免跳过中间尚未扫描的 PDF。
    列举屏障尚未清除时，也拒绝屏障上及其后的路径。
    FAILED 或 COMPLETED 时直接拒绝。续跑必须先释放主人、重开再认领。
    """
    roots = normalize_roots(source_roots)
    batch = db.batch(batch_no)
    if batch.scan_owner != worker:
        raise ScanBusy(batch_no)
    if batch.scan_status not in ("RUNNING", "INTERRUPTED"):
        raise BatchStateError(f"cannot advance status {batch.scan_status}")
    canon = lexical_candidate(raw_path, roots)
    if batch.cursor_path is None or path_lt(batch.cursor_path, canon):
        raise BatchStateError("cannot reregister a path ahead of the cursor")
    if batch.blocked_path is not None and not path_lt(canon, batch.blocked_path):
        raise BatchStateError("cannot pass an unlistable directory")
    st = lstat_regular(canon, roots)
    return _advance_new_or_existing(db, factory or DEFAULT_FACTORY, batch.id, worker, canon, st)


def register_candidates(
    db: MemoryRegistryDb,
    *,
    batch_no: str,
    source_roots: list[str],
    worker: str,
    raw_paths: list[str],
    factory: UuidV7Factory | None = None,
    stop_after: int | None = None,
) -> ScanSummary:
    """用调用方给出的路径登记。测试用它覆盖 ./、// 和 ..，不扫描其他目录。"""
    roots = normalize_roots(source_roots)
    db.ensure_batch(batch_no, roots)
    if db.claim(batch_no, worker) != 1:
        raise ScanBusy(batch_no)
    pdfs = [p for p in raw_paths if isinstance(p, str) and p.lower().endswith(".pdf")]
    return _process(db, batch_no, roots, worker, pdfs, factory or DEFAULT_FACTORY, stop_after)


def _require_real_dirs(roots: tuple[str, ...]) -> None:
    for root in roots:
        if os.path.islink(root) or not os.path.isdir(root):
            raise RetryableScanError(root)


def reopen_failed_batch(db: MemoryRegistryDb, batch_no: str):
    """FAILED → INTERRUPTED。不改游标、已登记 file_id，也不清目录列举屏障。"""
    return db.reopen_failed(batch_no)


def _process(
    db,
    batch_no,
    roots,
    worker,
    raws,
    factory: UuidV7Factory,
    stop_after,
    listing_errors=None,
    walked: bool = False,
    record_limit: int | None = None,
) -> ScanSummary:
    batch = db.batch(batch_no)
    listing_errors = list(listing_errors or [])
    if walked:
        earliest = min((path for path, _reason in listing_errors), key=path_sort_key) if listing_errors else None
        db.set_blocked_path(worker=worker, batch_id=batch.id, blocked_path=earliest)
    barrier = db.batch(batch_no).blocked_path
    items = []
    for raw in raws:
        try:
            canon = lexical_candidate(raw, roots)
            items.append((canon, None))
        except PathRejected as rejected:
            items.append((rejected.lexical or raw, rejected))
    items.sort(key=lambda item: path_sort_key(item[0]))
    if barrier is not None:
        # 不得把游标推进到未能列举的目录及其后的路径。
        items = [(key, rejected) for key, rejected in items if path_lt(key, barrier)]
    outcomes: list[RegisterOutcome] = []
    commits = 0
    stopped = False
    for index, (key, rejected) in enumerate(items):
        cursor = db.batch(batch_no).cursor_path
        if cursor is not None and not path_lt(cursor, key):
            continue
        if rejected is not None:
            if rejected.advance_cursor and rejected.lexical:
                if db.skip_and_advance(worker=worker, batch_id=batch.id, lexical=rejected.lexical):
                    commits += 1
            if _pause_for_later(db, batch, worker, items, index, commits, stop_after):
                stopped = True
                break
            continue
        try:
            st = lstat_regular(key, roots)
        except LstatFailed as failed:
            db.mark_failed(worker=worker, batch_id=batch.id, last_error=failed.lexical)
            break
        except PathRejected as rejected:
            if rejected.advance_cursor and rejected.lexical:
                if db.skip_and_advance(worker=worker, batch_id=batch.id, lexical=rejected.lexical):
                    commits += 1
            if _pause_for_later(db, batch, worker, items, index, commits, stop_after):
                stopped = True
                break
            continue
        try:
            st = stat_regular(key)
        except StatFailed as failed:
            if db.skip_stat_failure(worker=worker, batch_id=batch.id, lexical=failed.lexical):
                commits += 1
            if _pause_for_later(db, batch, worker, items, index, commits, stop_after):
                stopped = True
                break
            continue
        outcome = _advance_new_or_existing(db, factory, batch.id, worker, key, st)
        outcomes.append(outcome)
        if db.batch(batch_no).cursor_path == key:
            commits += 1
        if _pause_for_later(db, batch, worker, items, index, commits, stop_after):
            stopped = True
            break
        if _pause_for_record_limit(db, batch, worker, items, index, outcomes, record_limit):
            stopped = True
            break
    if not stopped and db.batch(batch_no).scan_status in ("RUNNING", "INTERRUPTED"):
        blocked = db.batch(batch_no).blocked_path
        if blocked is not None:
            if listing_errors:
                message = "\n".join(
                    f"{path}: {reason}"
                    for path, reason in sorted(listing_errors, key=lambda item: path_sort_key(item[0]))
                )
            else:
                message = db.batch(batch_no).last_error or f"{blocked}: directory listing failed"
            db.mark_failed(worker=worker, batch_id=batch.id, last_error=message)
        else:
            db.complete(worker=worker, batch_id=batch.id)
    current = db.batch(batch_no)
    return ScanSummary(
        batch_id=current.id,
        batch_no=current.batch_no,
        scan_status=current.scan_status,
        cursor_path=current.cursor_path,
        files_registered=current.files_registered,
        skipped_count=current.skipped_count,
        last_error=current.last_error,
        outcomes=outcomes,
        stat_errors=list(current.stat_errors),
    )


def _pause_for_record_limit(db, batch, worker, items, index, outcomes, record_limit) -> bool:
    """limit 只限制这次交出的 FileRecord 数。后面还有候选才把批次停成 INTERRUPTED。"""
    if record_limit is None or len(outcomes) < record_limit:
        return False
    cursor = db.batch(batch.batch_no).cursor_path
    for key, _rejected in items[index + 1 :]:
        if cursor is None or path_lt(cursor, key):
            db.interrupt(worker=worker, batch_id=batch.id)
            return True
    return False


def _pause_for_later(db, batch, worker, items, index, commits, stop_after) -> bool:
    if stop_after is None or commits < stop_after:
        return False
    cursor = db.batch(batch.batch_no).cursor_path
    for key, _rejected in items[index + 1 :]:
        if cursor is None or path_lt(cursor, key):
            db.interrupt(worker=worker, batch_id=batch.id)
            return True
    return False


def _advance_new_or_existing(db, factory: UuidV7Factory, batch_id, worker, canon, st) -> RegisterOutcome:
    discarded: list[str] = []
    nbytes = st.st_size
    mtime = mtime_utc(st)
    for _ in range(8):
        file_id = factory.new()
        try:
            result: AdvanceResult = db.register_and_advance(
                worker=worker,
                batch_id=batch_id,
                file_id=file_id,
                source_path=canon,
                nbytes=nbytes,
                mtime=mtime,
            )
        except FileIdCollision:
            discarded.append(file_id)
            continue
        if not result.inserted:
            discarded.append(file_id)
        return RegisterOutcome(result.row, result.inserted, discarded)
    raise RuntimeError("UUIDv7 primary-key retries exhausted")


class Registry:
    """进程内登记。scan 只读源目录，交出 FileRecord。不判断类别，不复制文件，不读写 seen。"""

    def __init__(
        self,
        db: MemoryRegistryDb | None = None,
        *,
        worker: str = "registry",
        factory: UuidV7Factory | None = None,
    ):
        self.db = db or MemoryRegistryDb()
        self.worker = worker
        self.factory = factory or DEFAULT_FACTORY
        self.stat_errors: list[str] = []
        self._open_batch: dict[str, str] = {}
        self._seq = 0

    def scan(self, root: str, limit: int = 10000) -> list[FileRecord]:
        """Registry.scan(root, limit) -> FileRecord 列表。

        同一未完成批次从 cursor_path 之后续扫。批次完成后再次调用会新开批次，
        读回已有 file_id，不改首次 batch_id。根目录不存在时抛 RetryableScanError。
        登记不计算字节哈希；内容版本由 confirm_content 在哈希确认后追加。
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("limit must be a non-negative int")
        if limit == 0:
            return []
        roots = normalize_roots([root])
        _require_real_dirs(roots)
        canon = roots[0]
        batch_no = self._prepare_batch(canon)
        summary = scan_batch(
            self.db,
            batch_no=batch_no,
            source_roots=[canon],
            worker=self.worker,
            factory=self.factory,
            record_limit=limit,
        )
        if summary.scan_status == "COMPLETED":
            self._open_batch.pop(canon, None)
        self.stat_errors = list(summary.stat_errors)
        return [outcome.row for outcome in summary.outcomes]

    def confirm_content(self, file_id: str, source_sha256: str | None, nbytes: int, mtime, batch_id: int) -> FileRecord:
        """内容字节哈希变化时只追加 content_version，file_id 保持第一次插入的那一枚。"""
        row, _appended = self.db.confirm_content(
            file_id=file_id,
            source_sha256=source_sha256,
            nbytes=nbytes,
            mtime=mtime,
            batch_id=batch_id,
        )
        return row

    def content_versions(self, file_id: str):
        return self.db.content_versions(file_id)

    def _prepare_batch(self, canon: str) -> str:
        batch_no = self._open_batch.get(canon)
        batch = self.db.find_batch(batch_no)
        if batch is None or batch.scan_status == "COMPLETED":
            self._seq += 1
            batch_no = f"scan-{self._seq}"
            self._open_batch[canon] = batch_no
            return batch_no
        if batch.scan_owner == self.worker and batch.scan_status in ("RUNNING", "INTERRUPTED"):
            self.db.release_owner(batch_no)
        return batch_no
