"""第四轮 QA 复现：游标跳跃、列举屏障、FAILED 中断、目录符号链接。"""
import os

import pytest

from registry.memory import MemoryRegistryDb
from registry.paths import path_lt
from registry.service import (
    BatchStateError,
    register_candidates,
    reopen_failed_batch,
    reregister,
    scan_batch,
)


def _snap(db: MemoryRegistryDb, batch_no: str):
    batch = db.batch(batch_no)
    rows = tuple(sorted((row.source_path, row.file_id) for row in db.rows()))
    return (
        batch.scan_status,
        batch.cursor_path,
        batch.files_registered,
        batch.skipped_count,
        batch.scan_owner,
        batch.last_error,
        batch.blocked_path,
        rows,
    )


def test_reregister_cannot_skip_unscanned_pdfs(tmp_path):
    root = tmp_path
    for name in ("a.pdf", "m.pdf", "z.pdf"):
        (root / name).write_bytes(f"%PDF-{name}".encode())
    db = MemoryRegistryDb()
    paused = scan_batch(
        db, batch_no="batch", source_roots=[str(root)], worker="w", stop_after=1
    )
    assert paused.scan_status == "INTERRUPTED"
    assert paused.cursor_path == str(root / "a.pdf")
    kept = db.get_by_path(str(root / "a.pdf")).file_id
    before = _snap(db, "batch")

    with pytest.raises(BatchStateError):
        reregister(
            db,
            batch_no="batch",
            source_roots=[str(root)],
            worker="w",
            raw_path=str(root / "z.pdf"),
        )
    assert _snap(db, "batch") == before

    db.release_owner("batch")
    resumed = scan_batch(db, batch_no="batch", source_roots=[str(root)], worker="w")
    finished = {row.source_path: row.file_id for row in db.rows()}
    assert resumed.scan_status == "COMPLETED"
    assert resumed.cursor_path == str(root / "z.pdf")
    assert set(finished) == {str(root / name) for name in ("a.pdf", "m.pdf", "z.pdf")}
    assert finished[str(root / "a.pdf")] == kept
    assert len(set(finished.values())) == 3
    assert resumed.files_registered == 3


def test_explicit_paths_cannot_cross_a_listing_barrier(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    (root / "a.pdf").write_bytes(b"%PDF-a")
    (root / "b.pdf").write_bytes(b"%PDF-b")
    (root / "z.pdf").write_bytes(b"%PDF-z")
    hole = root / "locked"
    hole.mkdir()
    (hole / "c.pdf").write_bytes(b"%PDF-c")
    os.chmod(hole, 0)
    db = MemoryRegistryDb()
    roots = [str(root)]
    try:
        paused = scan_batch(db, batch_no="batch", source_roots=roots, worker="w", stop_after=1)
        assert paused.scan_status == "INTERRUPTED"
        assert paused.cursor_path == str(root / "a.pdf")
        assert db.batch("batch").blocked_path == str(hole)
        kept = db.get_by_path(str(root / "a.pdf")).file_id
        before = _snap(db, "batch")
        with pytest.raises(BatchStateError):
            reregister(
                db,
                batch_no="batch",
                source_roots=roots,
                worker="w",
                raw_path=str(root / "z.pdf"),
            )
        assert _snap(db, "batch") == before
    finally:
        os.chmod(hole, 0o755)

    db.release_owner("batch")
    explicit = register_candidates(
        db,
        batch_no="batch",
        source_roots=roots,
        worker="w",
        raw_paths=[str(root / "b.pdf"), str(hole / "c.pdf"), str(root / "z.pdf")],
    )
    assert explicit.scan_status == "FAILED"
    assert explicit.cursor_path == str(root / "b.pdf")
    assert path_lt(explicit.cursor_path, str(hole))
    paths = {row.source_path for row in db.rows()}
    assert str(root / "b.pdf") in paths
    assert str(hole / "c.pdf") not in paths
    assert str(root / "z.pdf") not in paths
    assert db.get_by_path(str(root / "a.pdf")).file_id == kept

    db.release_owner("batch")
    reopen_failed_batch(db, "batch")
    recovered = scan_batch(db, batch_no="batch", source_roots=roots, worker="w")
    finished = {row.source_path: row.file_id for row in db.rows()}
    assert recovered.scan_status == "COMPLETED"
    assert db.batch("batch").blocked_path is None
    assert finished[str(root / "a.pdf")] == kept
    assert set(finished) == {
        str(root / "a.pdf"),
        str(root / "b.pdf"),
        str(hole / "c.pdf"),
        str(root / "z.pdf"),
    }
    assert len(set(finished.values())) == 4


def test_interrupt_cannot_turn_failed_into_interrupted(tmp_path):
    root = tmp_path
    (root / "a.pdf").write_bytes(b"%PDF-a")
    (root / "z.pdf").write_bytes(b"%PDF-z")
    raw_paths = [str(root / "a.pdf"), str(root / "m.pdf"), str(root / "z.pdf")]
    db = MemoryRegistryDb()
    failed = register_candidates(
        db, batch_no="batch", source_roots=[str(root)], worker="w", raw_paths=raw_paths
    )
    assert failed.scan_status == "FAILED"
    assert failed.cursor_path == str(root / "a.pdf")
    kept = db.get_by_path(str(root / "a.pdf")).file_id
    before = _snap(db, "batch")

    with pytest.raises(BatchStateError):
        db.interrupt(worker="w", batch_id=failed.batch_id)
    assert _snap(db, "batch") == before

    with pytest.raises(BatchStateError):
        reregister(
            db,
            batch_no="batch",
            source_roots=[str(root)],
            worker="w",
            raw_path=str(root / "z.pdf"),
        )
    assert _snap(db, "batch") == before

    (root / "m.pdf").write_bytes(b"%PDF-m")
    db.release_owner("batch")
    reopen_failed_batch(db, "batch")
    recovered = scan_batch(db, batch_no="batch", source_roots=[str(root)], worker="w")
    finished = {row.source_path: row.file_id for row in db.rows()}
    assert recovered.scan_status == "COMPLETED"
    assert set(finished) == {str(root / name) for name in ("a.pdf", "m.pdf", "z.pdf")}
    assert finished[str(root / "a.pdf")] == kept
    assert len(set(finished.values())) == 3


def test_intermediate_directory_symlink_cannot_register_outside_pdf(tmp_path):
    root = tmp_path / "src"
    outside = tmp_path / "outside"
    nested = outside / "nested"
    root.mkdir()
    nested.mkdir(parents=True)
    secret = nested / "secret.pdf"
    secret.write_bytes(b"%PDF-SECRET-OUTSIDE")
    (root / "a.pdf").write_bytes(b"%PDF-a")
    (root / "sub").mkdir()
    (root / "sub" / "link").symlink_to(outside, target_is_directory=True)
    escaped = root / "sub" / "link" / "nested" / "secret.pdf"
    db = MemoryRegistryDb()
    summary = register_candidates(
        db,
        batch_no="batch",
        source_roots=[str(root)],
        worker="w",
        raw_paths=[str(escaped), str(root / "a.pdf")],
    )
    assert summary.scan_status == "COMPLETED"
    assert {row.source_path for row in db.rows()} == {str(root / "a.pdf")}
    assert db.get_by_path(str(root / "a.pdf")).nbytes == len(b"%PDF-a")
    assert summary.files_registered == 1
    assert summary.skipped_count == 1
    kept = db.get_by_path(str(root / "a.pdf")).file_id

    again = scan_batch(db, batch_no="rescan", source_roots=[str(root)], worker="w")
    assert again.scan_status == "COMPLETED"
    assert {row.source_path for row in db.rows()} == {str(root / "a.pdf")}
    assert db.get_by_path(str(root / "a.pdf")).file_id == kept
