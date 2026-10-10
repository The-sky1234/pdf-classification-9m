"""lstat 与目录列举失败后的受控恢复。"""
import os

import pytest

from registry.memory import MemoryRegistryDb
from registry.paths import path_lt
from registry.service import (
    BatchStateError,
    ScanBusy,
    register_candidates,
    reopen_failed_batch,
    scan_batch,
)


def _canon(path) -> str:
    return os.path.normpath(str(path))


def _ids(db: MemoryRegistryDb) -> dict[str, str]:
    return {row.source_path: row.file_id for row in db.rows()}


def _release_and_reopen(db: MemoryRegistryDb, batch_no: str) -> None:
    db.release_owner(batch_no)
    reopen_failed_batch(db, batch_no)


def test_lstat_failure_can_fail_again_then_resume_without_new_file_ids(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    early = root / "a.pdf"
    missing = root / "m-gap.pdf"
    later = root / "z.pdf"
    early.write_bytes(b"%PDF-a")
    later.write_bytes(b"%PDF-z")
    raw_paths = [_canon(early), _canon(missing), _canon(later)]
    roots = [_canon(root)]
    db = MemoryRegistryDb()

    failed = register_candidates(
        db, batch_no="batch", source_roots=roots, worker="worker", raw_paths=raw_paths
    )
    assert failed.scan_status == "FAILED"
    assert failed.last_error == _canon(missing)
    assert failed.cursor_path == _canon(early)
    assert failed.files_registered == 1
    kept = _ids(db)
    assert list(kept) == [_canon(early)]

    with pytest.raises(ScanBusy):
        register_candidates(
            db, batch_no="batch", source_roots=roots, worker="worker", raw_paths=raw_paths
        )
    with pytest.raises(BatchStateError):
        reopen_failed_batch(db, "batch")
    assert db.batch("batch").scan_status == "FAILED"
    assert db.batch("batch").cursor_path == _canon(early)
    assert _ids(db) == kept

    db.release_owner("batch")
    assert db.claim("batch", "other") == 0
    reopened = reopen_failed_batch(db, "batch")
    assert reopened.scan_status == "INTERRUPTED"
    assert reopened.cursor_path == _canon(early)
    assert reopened.files_registered == 1
    assert reopened.last_error == _canon(missing)
    assert _ids(db) == kept

    failed_again = register_candidates(
        db, batch_no="batch", source_roots=roots, worker="worker", raw_paths=raw_paths
    )
    assert failed_again.scan_status == "FAILED"
    assert failed_again.cursor_path == _canon(early)
    assert failed_again.files_registered == 1
    assert failed_again.last_error == _canon(missing)
    assert _ids(db) == kept

    missing.write_bytes(b"%PDF-gap")
    _release_and_reopen(db, "batch")
    recovered = register_candidates(
        db, batch_no="batch", source_roots=roots, worker="worker", raw_paths=raw_paths
    )
    assert recovered.scan_status == "COMPLETED"
    assert recovered.last_error is None
    assert recovered.cursor_path == _canon(later)
    assert recovered.files_registered == 3
    finished = _ids(db)
    assert finished[_canon(early)] == kept[_canon(early)]
    assert set(finished) == {_canon(early), _canon(missing), _canon(later)}
    assert len(set(finished.values())) == 3

    with pytest.raises(BatchStateError):
        reopen_failed_batch(db, "batch")
    assert db.batch("batch").scan_status == "COMPLETED"
    assert db.batch("batch").cursor_path == _canon(later)

    for index in range(3):
        again = scan_batch(db, batch_no=f"rescan-{index}", source_roots=roots, worker="worker")
        assert again.scan_status == "COMPLETED"
        assert _ids(db) == finished
        assert len(db.rows()) == 3


def test_lstat_failure_on_the_first_path_retries_that_path_then_continues(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    missing = root / "a.pdf"
    later = root / "b.pdf"
    later.write_bytes(b"%PDF-b")
    raw_paths = [_canon(missing), _canon(later)]
    roots = [_canon(root)]
    db = MemoryRegistryDb()

    failed = register_candidates(
        db, batch_no="batch", source_roots=roots, worker="worker", raw_paths=raw_paths
    )
    assert failed.scan_status == "FAILED"
    assert failed.cursor_path is None
    assert failed.files_registered == 0
    assert db.rows() == []

    _release_and_reopen(db, "batch")
    failed_again = register_candidates(
        db, batch_no="batch", source_roots=roots, worker="worker", raw_paths=raw_paths
    )
    assert failed_again.scan_status == "FAILED"
    assert failed_again.cursor_path is None
    assert db.rows() == []

    missing.write_bytes(b"%PDF-a")
    _release_and_reopen(db, "batch")
    recovered = register_candidates(
        db, batch_no="batch", source_roots=roots, worker="worker", raw_paths=raw_paths
    )
    assert recovered.scan_status == "COMPLETED"
    assert recovered.cursor_path == _canon(later)
    finished = _ids(db)
    assert set(finished) == {_canon(missing), _canon(later)}
    assert len(set(finished.values())) == 2
    again = scan_batch(db, batch_no="rescan", source_roots=roots, worker="worker")
    assert again.scan_status == "COMPLETED"
    assert _ids(db) == finished


def test_unlistable_root_is_failed_and_recovers_after_permission_returns(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    pdf = root / "a.pdf"
    pdf.write_bytes(b"%PDF-a")
    db = MemoryRegistryDb()
    roots = [_canon(root)]
    os.chmod(root, 0)
    try:
        failed = scan_batch(db, batch_no="batch", source_roots=roots, worker="worker")
    finally:
        os.chmod(root, 0o755)

    assert failed.scan_status == "FAILED"
    assert failed.scan_status != "COMPLETED"
    assert _canon(root) in (failed.last_error or "")
    assert failed.cursor_path is None
    assert db.rows() == []

    _release_and_reopen(db, "batch")
    recovered = scan_batch(db, batch_no="batch", source_roots=roots, worker="worker")
    assert recovered.scan_status == "COMPLETED"
    assert recovered.last_error is None
    first_ids = _ids(db)
    assert list(first_ids) == [_canon(pdf)]

    for index in range(2):
        again = scan_batch(db, batch_no=f"rescan-{index}", source_roots=roots, worker="worker")
        assert again.scan_status == "COMPLETED"
        assert _ids(db) == first_ids


def test_unlistable_child_does_not_advance_cursor_past_the_hole(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    early = root / "a.pdf"
    first_hole = root / "locked-a"
    between = root / "locked-a0.pdf"
    second_hole = root / "locked-b"
    later = root / "z.pdf"
    early.write_bytes(b"%PDF-a")
    between.write_bytes(b"%PDF-between")
    later.write_bytes(b"%PDF-z")
    first_hole.mkdir()
    second_hole.mkdir()
    (first_hole / "c.pdf").write_bytes(b"%PDF-c")
    (second_hole / "d.pdf").write_bytes(b"%PDF-d")
    os.chmod(first_hole, 0)
    os.chmod(second_hole, 0)
    roots = [_canon(root)]
    db = MemoryRegistryDb()
    try:
        failed = scan_batch(db, batch_no="batch", source_roots=roots, worker="worker")
        assert failed.scan_status == "FAILED"
        assert failed.cursor_path == _canon(early)
        assert path_lt(failed.cursor_path, _canon(first_hole))
        assert _canon(first_hole) in failed.last_error
        assert _canon(second_hole) in failed.last_error
        assert _ids(db) == {_canon(early): _ids(db)[_canon(early)]}
        kept = _ids(db)[_canon(early)]

        _release_and_reopen(db, "batch")
        failed_again = scan_batch(db, batch_no="batch", source_roots=roots, worker="worker")
        assert failed_again.scan_status == "FAILED"
        assert failed_again.cursor_path == _canon(early)
        assert path_lt(failed_again.cursor_path, _canon(first_hole))
        assert list(_ids(db)) == [_canon(early)]
        assert _ids(db)[_canon(early)] == kept
    finally:
        os.chmod(first_hole, 0o755)
        os.chmod(second_hole, 0o755)

    _release_and_reopen(db, "batch")
    recovered = scan_batch(db, batch_no="batch", source_roots=roots, worker="worker")
    assert recovered.scan_status == "COMPLETED"
    assert recovered.last_error is None
    finished = _ids(db)
    assert finished[_canon(early)] == kept
    assert set(finished) == {
        _canon(early),
        _canon(first_hole / "c.pdf"),
        _canon(between),
        _canon(second_hole / "d.pdf"),
        _canon(later),
    }
    assert len(set(finished.values())) == 5
    assert recovered.cursor_path == _canon(later)
    assert recovered.files_registered == 5

    for index in range(3):
        again = scan_batch(db, batch_no=f"rescan-{index}", source_roots=roots, worker="worker")
        assert again.scan_status == "COMPLETED"
        assert _ids(db) == finished
        assert len(db.rows()) == 5


def test_partial_scan_does_not_complete_while_a_child_directory_is_unlistable(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    early = root / "a.pdf"
    middle = root / "b.pdf"
    hole = root / "locked"
    later = root / "z.pdf"
    early.write_bytes(b"%PDF-a")
    middle.write_bytes(b"%PDF-b")
    later.write_bytes(b"%PDF-z")
    hole.mkdir()
    (hole / "c.pdf").write_bytes(b"%PDF-c")
    os.chmod(hole, 0)
    roots = [_canon(root)]
    db = MemoryRegistryDb()
    try:
        partial = scan_batch(
            db, batch_no="batch", source_roots=roots, worker="worker", stop_after=1
        )
        assert partial.scan_status == "INTERRUPTED"
        assert partial.cursor_path == _canon(early)
        assert list(_ids(db)) == [_canon(early)]
        kept = _ids(db)[_canon(early)]

        db.release_owner("batch")
        blocked = scan_batch(db, batch_no="batch", source_roots=roots, worker="worker")
        assert blocked.scan_status == "FAILED"
        assert blocked.cursor_path == _canon(middle)
        assert path_lt(blocked.cursor_path, _canon(hole))
        assert _canon(later) not in _ids(db)
        assert _canon(hole / "c.pdf") not in _ids(db)
        assert _ids(db)[_canon(early)] == kept
    finally:
        os.chmod(hole, 0o755)

    _release_and_reopen(db, "batch")
    recovered = scan_batch(db, batch_no="batch", source_roots=roots, worker="worker")
    assert recovered.scan_status == "COMPLETED"
    finished = _ids(db)
    assert finished[_canon(early)] == kept
    assert set(finished) == {
        _canon(early),
        _canon(middle),
        _canon(hole / "c.pdf"),
        _canon(later),
    }
    assert len(set(finished.values())) == 4
    assert recovered.files_registered == 4
    again = scan_batch(db, batch_no="rescan", source_roots=roots, worker="worker")
    assert again.scan_status == "COMPLETED"
    assert _ids(db) == finished


def test_explicit_paths_cannot_move_cursor_past_an_unlisted_directory(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    early = root / "a.pdf"
    hole = root / "locked"
    later = root / "z.pdf"
    early.write_bytes(b"%PDF-a")
    later.write_bytes(b"%PDF-z")
    hole.mkdir()
    (hole / "c.pdf").write_bytes(b"%PDF-c")
    os.chmod(hole, 0)
    roots = [_canon(root)]
    db = MemoryRegistryDb()
    try:
        failed = scan_batch(db, batch_no="batch", source_roots=roots, worker="worker")
        assert failed.scan_status == "FAILED"
        assert db.batch("batch").blocked_path == _canon(hole)
        kept = _ids(db)[_canon(early)]
    finally:
        os.chmod(hole, 0o755)

    _release_and_reopen(db, "batch")
    assert db.batch("batch").blocked_path == _canon(hole)
    assert db.batch("batch").cursor_path == _canon(early)
    jumped = register_candidates(
        db,
        batch_no="batch",
        source_roots=roots,
        worker="worker",
        raw_paths=[_canon(early), _canon(later)],
    )
    assert jumped.scan_status == "FAILED"
    assert jumped.cursor_path == _canon(early)
    assert path_lt(jumped.cursor_path, _canon(hole))
    assert list(_ids(db)) == [_canon(early)]
    assert _ids(db)[_canon(early)] == kept

    _release_and_reopen(db, "batch")
    recovered = scan_batch(db, batch_no="batch", source_roots=roots, worker="worker")
    assert recovered.scan_status == "COMPLETED"
    assert db.batch("batch").blocked_path is None
    finished = _ids(db)
    assert finished[_canon(early)] == kept
    assert set(finished) == {_canon(early), _canon(hole / "c.pdf"), _canon(later)}
    assert len(set(finished.values())) == 3
