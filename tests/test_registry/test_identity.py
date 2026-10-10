"""Registry 第一阶段：UUIDv7、规范路径、重复扫描幂等。使用临时目录和内存库。"""
from __future__ import annotations

import hashlib
import re
import threading
import uuid

import pytest

from registry.memory import MemoryRegistryDb
from registry.paths import mtime_utc
from registry.service import (
    RetryableScanError,
    ScanBusy,
    register_candidates,
    register_file,
    reregister,
    scan_batch,
)
from registry.uuidv7 import V7_RE_TEXT, UuidV7Factory, timestamp_prefix

V7_RE = re.compile(V7_RE_TEXT)


def _factory(ms=1_700_000_000_000):
    clock = {"ms": ms}

    def now():
        return clock["ms"]

    def sleep(_seconds):
        clock["ms"] += 1

    return UuidV7Factory(clock_ms=now, sleep=sleep), clock


def test_uuid_v7_monotonic_and_clock_rollback():
    factory, clock = _factory()
    issued = [factory.new() for _ in range(5)]
    assert issued == sorted(issued)
    assert all(V7_RE.fullmatch(item) for item in issued)
    assert len(set(timestamp_prefix(item) for item in issued)) == 1
    for earlier, later in zip(issued, issued[1:]):
        assert earlier < later
    previous = timestamp_prefix(issued[-1])
    clock["ms"] -= 10_000
    rolled = factory.new()
    assert V7_RE.fullmatch(rolled)
    assert timestamp_prefix(rolled) >= previous
    assert rolled > issued[-1]


def test_uuid_v7_rand_a_overflow_waits_for_next_millisecond():
    clock = {"ms": 50}

    def randbits(n):
        return 4095 if n == 12 else 0

    factory = UuidV7Factory(
        clock_ms=lambda: clock["ms"],
        sleep=lambda _seconds: clock.__setitem__("ms", clock["ms"] + 1),
        randbits=randbits,
    )
    first = factory.new()
    second = factory.new()
    assert timestamp_prefix(second) > timestamp_prefix(first)
    assert second > first


def test_spellings_share_one_v7_id_and_rescan_is_idempotent(tmp_path):
    root = str(tmp_path)
    target = tmp_path / "a.pdf"
    target.write_bytes(b"%PDF-1")
    (tmp_path / "sub").mkdir()
    spellings = [f"{root}/./a.pdf", f"{root}//a.pdf", f"{root}/sub/../a.pdf"]
    db = MemoryRegistryDb()
    factory, _clock = _factory()
    first = register_candidates(
        db, batch_no="b1", source_roots=[root], worker="w", raw_paths=spellings, factory=factory
    )
    row = db.get_by_path(f"{root}/a.pdf")
    assert row is not None
    assert first.files_registered == 1
    assert first.cursor_path == f"{root}/a.pdf"
    assert len(db.rows()) == 1
    assert row.id_origin == "V7"
    assert row.source_sha256 is None
    assert row.content_version is None
    assert row.nbytes == 6
    assert V7_RE.fullmatch(row.file_id)
    path_hash = hashlib.sha256(row.source_path.encode()).hexdigest()[:32]
    assert row.file_id != str(uuid.uuid5(uuid.NAMESPACE_URL, "file:" + row.source_path))
    assert row.file_id != uuid.uuid5(uuid.NAMESPACE_URL, row.source_path + ":" + path_hash).hex
    target.write_bytes(b"%PDF-1-changed")
    second = register_candidates(
        db, batch_no="b2", source_roots=[root + "/"], worker="w", raw_paths=spellings, factory=factory
    )
    again = db.get_by_path(f"{root}/a.pdf")
    assert again.file_id == row.file_id
    assert again.batch_id == row.batch_id
    assert again.id_origin == "V7"
    assert again.nbytes == 6
    assert again.mtime == row.mtime
    assert second.files_registered == 1
    assert second.batch_id != first.batch_id
    assert len(db.rows()) == 1
    assert all(discarded not in {item.file_id for item in db.rows()} for outcome in second.outcomes for discarded in outcome.discarded_file_ids)


def test_root_escape_relative_and_sibling_prefix_insert_nothing():
    db = MemoryRegistryDb()
    summary = register_candidates(
        db,
        batch_no="b",
        source_roots=["/data/pdfs"],
        worker="w",
        raw_paths=["/data/pdfs/../secret/a.pdf", "/data/pdfs2/a.pdf", "rel/a.pdf", ""],
        factory=_factory()[0],
    )
    assert db.rows() == []
    assert summary.files_registered == 0
    assert summary.skipped_count == 0
    assert summary.cursor_path is None
    assert summary.scan_status == "COMPLETED"


def test_surrogate_advances_cursor_without_file_id():
    db = MemoryRegistryDb()
    raw = "/data/pdfs/a\udc80.pdf"
    summary = register_candidates(
        db,
        batch_no="b",
        source_roots=["/data/pdfs"],
        worker="w",
        raw_paths=[raw],
        factory=_factory()[0],
    )
    assert db.rows() == []
    assert summary.skipped_count == 1
    assert summary.files_registered == 0
    assert summary.cursor_path == raw


def test_symlink_case_and_directory_link(tmp_path):
    real = tmp_path / "sub"
    real.mkdir()
    (real / "a.pdf").write_bytes(b"%PDF-a")
    (tmp_path / "A.pdf").write_bytes(b"%PDF-A")
    (tmp_path / "b.pdf").symlink_to(real / "a.pdf")
    (tmp_path / "link").symlink_to(real, target_is_directory=True)
    db = MemoryRegistryDb()
    summary = scan_batch(
        db, batch_no="b", source_roots=[str(tmp_path)], worker="w", factory=_factory()[0]
    )
    paths = {row.source_path for row in db.rows()}
    assert paths == {str(real / "a.pdf"), str(tmp_path / "A.pdf")}
    assert len({row.file_id for row in db.rows()}) == 2
    assert all(row.source_sha256 is None and row.id_origin == "V7" for row in db.rows())
    assert summary.skipped_count == 1
    assert summary.files_registered == 2
    assert summary.scan_status == "COMPLETED"
    assert not any(row.source_path.startswith(str(tmp_path / "link")) for row in db.rows())


def test_same_bytes_keep_distinct_ids(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"%PDF-same")
    (tmp_path / "b.pdf").write_bytes(b"%PDF-same")
    db = MemoryRegistryDb()
    scan_batch(db, batch_no="b", source_roots=[str(tmp_path)], worker="w", factory=_factory()[0])
    rows = db.rows()
    assert len(rows) == 2
    assert rows[0].file_id != rows[1].file_id
    assert rows[0].nbytes == rows[1].nbytes


def test_missing_root_is_retryable_and_missing_file_does_not_move_cursor(tmp_path):
    db = MemoryRegistryDb()
    with pytest.raises(RetryableScanError):
        scan_batch(db, batch_no="missing", source_roots=["/no/such/pdf-root"], worker="w")
    assert db._batches == {}
    summary = register_candidates(
        db,
        batch_no="gone",
        source_roots=[str(tmp_path)],
        worker="w",
        raw_paths=[str(tmp_path / "missing.pdf")],
        factory=_factory()[0],
    )
    assert summary.scan_status == "FAILED"
    assert summary.cursor_path is None
    assert summary.last_error == str(tmp_path / "missing.pdf")
    assert db.rows() == []


def test_interrupt_resume_skips_paths_behind_cursor(tmp_path):
    for name in ("a.pdf", "c.pdf"):
        (tmp_path / name).write_bytes(b"%PDF-" + name.encode())
    db = MemoryRegistryDb()
    factory, _clock = _factory()
    root = str(tmp_path)
    first = scan_batch(
        db, batch_no="run", source_roots=[root], worker="w", factory=factory, stop_after=1
    )
    assert first.scan_status == "INTERRUPTED"
    assert first.files_registered == 1
    assert first.cursor_path == f"{root}/a.pdf"
    assert len(db.rows()) == 1
    with pytest.raises(ScanBusy):
        scan_batch(db, batch_no="run", source_roots=[root], worker="other", factory=factory)
    (tmp_path / "0.pdf").write_bytes(b"%PDF-0")
    (tmp_path / "b.pdf").write_bytes(b"%PDF-b")
    db.release_owner("run")
    second = scan_batch(db, batch_no="run", source_roots=[root], worker="w", factory=factory)
    paths = {row.source_path for row in db.rows()}
    assert paths == {f"{root}/a.pdf", f"{root}/b.pdf", f"{root}/c.pdf"}
    assert f"{root}/0.pdf" not in paths
    assert second.files_registered == 3
    assert second.cursor_path == f"{root}/c.pdf"
    assert second.scan_status == "COMPLETED"
    a_id = db.get_by_path(f"{root}/a.pdf").file_id
    caught_up = scan_batch(db, batch_no="full", source_roots=[root], worker="w", factory=factory)
    assert db.get_by_path(f"{root}/a.pdf").file_id == a_id
    assert db.get_by_path(f"{root}/a.pdf").batch_id == first.batch_id
    assert db.get_by_path(f"{root}/0.pdf").batch_id == caught_up.batch_id
    assert len(db.rows()) == 4


def test_replay_does_not_increment_or_touch_seen(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"%PDF-a")
    (tmp_path / "b.pdf").write_bytes(b"%PDF-b")
    db = MemoryRegistryDb()
    root = str(tmp_path)
    factory, _clock = _factory()
    scan_batch(db, batch_no="run", source_roots=[root], worker="w", factory=factory, stop_after=1)
    seen = {"task-1"}
    outcome = reregister(
        db,
        batch_no="run",
        source_roots=[root],
        worker="w",
        raw_path=f"{root}/./a.pdf",
        factory=factory,
    )
    assert seen == {"task-1"}
    assert not outcome.inserted
    assert outcome.discarded_file_ids
    assert db.get_by_id(outcome.discarded_file_ids[0]) is None
    assert db.batch("run").files_registered == 1
    assert db.batch("run").cursor_path == f"{root}/a.pdf"
    assert len(db.rows()) == 1


def test_legacy_id_is_not_rewritten(tmp_path):
    target = tmp_path / "old.pdf"
    target.write_bytes(b"%PDF-old")
    db = MemoryRegistryDb()
    root = str(tmp_path)
    from registry.service import normalize_roots

    legacy_batch = db.ensure_batch("legacy", normalize_roots([root]))
    legacy_id = "ab" * 16
    st = target.stat()
    db.seed_legacy(
        file_id=legacy_id,
        source_path=str(target),
        nbytes=st.st_size,
        mtime=mtime_utc(st),
        batch_id=legacy_batch.id,
    )
    summary = register_candidates(
        db,
        batch_no="rescan",
        source_roots=[root],
        worker="w",
        raw_paths=[str(target)],
        factory=_factory()[0],
    )
    row = db.get_by_path(str(target))
    assert row.file_id == legacy_id
    assert row.id_origin == "LEGACY"
    assert row.batch_id == legacy_batch.id
    assert summary.files_registered == 1
    assert len(db.rows()) == 1
    assert db.get_by_id(summary.outcomes[0].discarded_file_ids[0]) is None


def test_concurrent_insert_keeps_one_row():
    db = MemoryRegistryDb()
    batch = db.ensure_batch("c", ("/data/pdfs",))
    factory, _clock = _factory()
    canon = "/data/pdfs/a.pdf"
    barrier = threading.Barrier(2)
    found = []

    def work():
        barrier.wait()
        found.append(
            register_file(
                db,
                source_path=canon,
                nbytes=4,
                mtime=mtime_utc(type("S", (), {"st_mtime_ns": 1_000})()),
                batch_id=batch.id,
                factory=factory,
            )
        )

    threads = [threading.Thread(target=work) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(db.rows()) == 1
    assert found[0].row.file_id == found[1].row.file_id
    discarded = [item for outcome in found for item in outcome.discarded_file_ids]
    assert discarded
    assert all(db.get_by_id(item) is None for item in discarded)
    assert found[0].row.id_origin == "V7"
