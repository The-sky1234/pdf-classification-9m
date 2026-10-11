"""第三轮 QA 复现：双前导斜杠、FAILED 推进游标、非法完成。"""
import pytest

from registry.memory import MemoryRegistryDb
from registry.service import (
    BatchStateError,
    register_candidates,
    reopen_failed_batch,
    reregister,
    scan_batch,
)


def _snap(db: MemoryRegistryDb, batch_no: str):
    batch = db.batch(batch_no)
    rows = tuple(sorted((row.source_path, row.file_id, row.batch_id) for row in db.rows()))
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


def test_double_leading_slash_is_one_source_path_without_merging_distinct_files(tmp_path):
    docs = tmp_path / "docs"
    other_case = tmp_path / "Docs"
    docs.mkdir()
    other_case.mkdir()
    (docs / "a.pdf").write_bytes(b"%PDF-a")
    (docs / "b.pdf").write_bytes(b"%PDF-b")
    (other_case / "A.pdf").write_bytes(b"%PDF-A")
    root = str(docs)
    doubled = "//" + root.lstrip("/")
    db = MemoryRegistryDb()

    first = register_candidates(
        db,
        batch_no="plain",
        source_roots=[root],
        worker="w",
        raw_paths=[f"{root}/a.pdf", f"{root}/b.pdf"],
    )
    register_candidates(
        db,
        batch_no="doubled",
        source_roots=[doubled],
        worker="w",
        raw_paths=[f"{doubled}//a.pdf"],
    )
    register_candidates(
        db,
        batch_no="case",
        source_roots=[str(other_case)],
        worker="w",
        raw_paths=[str(other_case / "A.pdf")],
    )

    rows = {row.source_path: row for row in db.rows()}
    assert f"{doubled}/a.pdf" not in rows
    assert doubled not in {batch.source_roots[0] for batch in (db.batch("plain"), db.batch("doubled"))}
    assert rows[f"{root}/a.pdf"].file_id != rows[f"{root}/b.pdf"].file_id
    assert rows[f"{root}/a.pdf"].file_id != rows[str(other_case / "A.pdf")].file_id
    assert rows[f"{root}/a.pdf"].source_path != rows[str(other_case / "A.pdf")].source_path
    assert rows[f"{root}/a.pdf"].batch_id == first.batch_id
    assert len(rows) == 3
    assert all(not path.startswith("//") for path in rows)


def test_reregister_while_failed_cannot_skip_m_pdf(tmp_path):
    root = tmp_path
    (root / "a.pdf").write_bytes(b"%PDF-a")
    (root / "z.pdf").write_bytes(b"%PDF-z")
    paths = [str(root / "a.pdf"), str(root / "m.pdf"), str(root / "z.pdf")]
    db = MemoryRegistryDb()
    failed = register_candidates(
        db, batch_no="batch", source_roots=[str(root)], worker="w", raw_paths=paths
    )
    assert failed.scan_status == "FAILED"
    assert failed.cursor_path == str(root / "a.pdf")
    assert failed.last_error == str(root / "m.pdf")
    assert [row.source_path for row in db.rows()] == [str(root / "a.pdf")]
    before = _snap(db, "batch")
    kept = db.get_by_path(str(root / "a.pdf")).file_id

    with pytest.raises(BatchStateError):
        reregister(
            db,
            batch_no="batch",
            source_roots=[str(root)],
            worker="w",
            raw_path=str(root / "z.pdf"),
        )
    assert _snap(db, "batch") == before
    assert db.batch("batch").scan_owner == "w"

    (root / "m.pdf").write_bytes(b"%PDF-m")
    with pytest.raises(BatchStateError):
        reregister(
            db,
            batch_no="batch",
            source_roots=[str(root)],
            worker="w",
            raw_path=str(root / "m.pdf"),
        )
    assert _snap(db, "batch") == before

    db.release_owner("batch")
    reopen_failed_batch(db, "batch")
    recovered = register_candidates(
        db, batch_no="batch", source_roots=[str(root)], worker="w", raw_paths=paths
    )
    finished = {row.source_path: row.file_id for row in db.rows()}
    assert recovered.scan_status == "COMPLETED"
    assert recovered.cursor_path == str(root / "z.pdf")
    assert set(finished) == {str(root / "a.pdf"), str(root / "m.pdf"), str(root / "z.pdf")}
    assert finished[str(root / "a.pdf")] == kept
    assert len(set(finished.values())) == 3
    assert recovered.files_registered == 3


def test_complete_rejects_failed_interrupted_and_blocked_path(tmp_path):
    root = tmp_path
    (root / "a.pdf").write_bytes(b"%PDF-a")
    (root / "z.pdf").write_bytes(b"%PDF-z")
    db = MemoryRegistryDb()
    failed = register_candidates(
        db,
        batch_no="failed",
        source_roots=[str(root)],
        worker="w",
        raw_paths=[str(root / "a.pdf"), str(root / "m.pdf"), str(root / "z.pdf")],
    )
    assert failed.scan_status == "FAILED"
    kept = db.get_by_path(str(root / "a.pdf")).file_id
    failed_before = _snap(db, "failed")
    with pytest.raises(BatchStateError):
        db.complete(worker="w", batch_id=failed.batch_id)
    assert _snap(db, "failed") == failed_before

    paused = scan_batch(
        db, batch_no="paused", source_roots=[str(root)], worker="w", stop_after=1
    )
    assert paused.scan_status == "INTERRUPTED"
    paused_before = _snap(db, "paused")
    with pytest.raises(BatchStateError):
        db.complete(worker="w", batch_id=paused.batch_id)
    assert _snap(db, "paused") == paused_before
    assert db.get_by_path(str(root / "a.pdf")).file_id == kept

    blocked = db.ensure_batch("blocked", ("/data/pdfs",))
    assert db.claim("blocked", "w") == 1
    db.set_blocked_path(worker="w", batch_id=blocked.id, blocked_path="/data/pdfs/locked")
    blocked_before = _snap(db, "blocked")
    with pytest.raises(BatchStateError):
        db.complete(worker="w", batch_id=blocked.id)
    assert _snap(db, "blocked") == blocked_before
    assert db.batch("blocked").scan_status == "RUNNING"

    db.release_owner("paused")
    resumed = scan_batch(db, batch_no="paused", source_roots=[str(root)], worker="w")
    assert resumed.scan_status == "COMPLETED"
    assert db.get_by_path(str(root / "a.pdf")).file_id == kept
    assert db.get_by_path(str(root / "z.pdf")) is not None
    assert db.get_by_path(str(root / "a.pdf")).file_id != db.get_by_path(str(root / "z.pdf")).file_id
