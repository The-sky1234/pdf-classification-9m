"""4.1 还缺的部分：Registry.scan、内容版本、单个文件 stat 失败。"""
from __future__ import annotations

import hashlib
import inspect
import os
import re
from datetime import datetime, timezone

import pytest

from registry.memory import ContentRejected
from registry.service import Registry, RetryableScanError, ScanBusy
from registry.uuidv7 import V7_RE_TEXT, UuidV7Factory

V7_RE = re.compile(V7_RE_TEXT)


def _factory():
    clock = {"ms": 1_700_000_000_000}

    def now():
        return clock["ms"]

    def sleep(_seconds):
        clock["ms"] += 1

    return UuidV7Factory(clock_ms=now, sleep=sleep)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_scan_returns_file_records_and_stops_at_limit(tmp_path):
    root = tmp_path
    for name in ("a.pdf", "b.pdf", "c.pdf"):
        (root / name).write_bytes(b"%PDF-" + name.encode())
    seen = {"task-1"}
    reg = Registry(factory=_factory())
    first = reg.scan(str(root), 1)
    assert seen == {"task-1"}
    assert len(first) == 1
    assert first[0].source_path == str(root / "a.pdf")
    assert first[0].id_origin == "V7"
    assert V7_RE.fullmatch(first[0].file_id)
    assert len(first[0].file_id) == 36
    assert isinstance(first[0].batch_id, int)
    assert first[0].content_version is None
    assert first[0].source_sha256 is None
    assert reg.db.batch("scan-1").scan_status == "INTERRUPTED"
    assert reg.db.batch("scan-1").cursor_path == str(root / "a.pdf")

    (root / "0.pdf").write_bytes(b"%PDF-0")
    second = reg.scan(str(root), 10)
    assert [row.source_path for row in second] == [str(root / "b.pdf"), str(root / "c.pdf")]
    assert reg.db.get_by_path(str(root / "0.pdf")) is None
    assert reg.db.batch("scan-1").scan_status == "COMPLETED"
    assert reg.db.batch("scan-1").files_registered == 3
    assert reg.db.batch("scan-1").cursor_path == str(root / "c.pdf")

    third = reg.scan(str(root), 10)
    paths = {row.source_path: row for row in third}
    assert set(paths) == {str(root / name) for name in ("0.pdf", "a.pdf", "b.pdf", "c.pdf")}
    assert paths[str(root / "a.pdf")].file_id == first[0].file_id
    assert paths[str(root / "a.pdf")].batch_id == first[0].batch_id
    assert paths[str(root / "0.pdf")].file_id != first[0].file_id
    assert paths[str(root / "0.pdf")].batch_id != first[0].batch_id
    assert len({row.file_id for row in reg.db.rows()}) == 4


def test_scan_limit_zero_and_missing_root_is_retryable(tmp_path):
    reg = Registry(factory=_factory())
    assert reg.scan(str(tmp_path), 0) == []
    assert reg.db.rows() == []
    missing = tmp_path / "gone"
    with pytest.raises(RetryableScanError):
        reg.scan(str(missing), 10)
    assert reg.db.rows() == []
    assert reg.db.find_batch("scan-1") is None
    missing.mkdir()
    (missing / "a.pdf").write_bytes(b"%PDF-a")
    found = reg.scan(str(missing), 10)
    assert len(found) == 1
    assert found[0].source_path == str(missing / "a.pdf")


def test_scan_does_not_copy_or_classify(tmp_path):
    target = tmp_path / "a.pdf"
    target.write_bytes(b"%PDF-a")
    before = sorted(path.name for path in tmp_path.iterdir())
    reg = Registry(factory=_factory())
    rows = reg.scan(str(tmp_path), 10)
    assert sorted(path.name for path in tmp_path.iterdir()) == before
    assert target.read_bytes() == b"%PDF-a"
    assert not hasattr(rows[0], "archive_type")
    assert rows[0].nbytes == len(b"%PDF-a")


def test_same_bytes_keep_distinct_file_ids_after_hash_confirm(tmp_path):
    blob = b"%PDF-same"
    (tmp_path / "a.pdf").write_bytes(blob)
    (tmp_path / "b.pdf").write_bytes(blob)
    reg = Registry(factory=_factory())
    rows = reg.scan(str(tmp_path), 10)
    assert len(rows) == 2
    assert rows[0].file_id != rows[1].file_id
    digest = _sha(blob)
    observed = datetime(2024, 1, 2, tzinfo=timezone.utc)
    for row in rows:
        reg.confirm_content(row.file_id, digest, len(blob), observed, row.batch_id)
    stored = reg.db.rows()
    assert stored[0].file_id != stored[1].file_id
    assert stored[0].source_sha256 == stored[1].source_sha256 == digest
    assert {row.content_version for row in stored} == {1}


def test_content_change_appends_version_and_keeps_file_id(tmp_path):
    target = tmp_path / "a.pdf"
    original = b"%PDF-v1"
    target.write_bytes(original)
    reg = Registry(factory=_factory())
    first = reg.scan(str(tmp_path), 10)
    row = first[0]
    assert row.content_version is None
    assert row.source_sha256 is None
    created_at = row.created_at
    origin_batch = row.batch_id
    file_id = row.file_id

    target.write_bytes(b"%PDF-v1-changed-on-disk")
    rescanned = reg.scan(str(tmp_path), 10)
    assert rescanned[0].file_id == file_id
    assert rescanned[0].content_version is None
    assert rescanned[0].nbytes == len(original)
    assert rescanned[0].batch_id == origin_batch

    t1 = datetime(2024, 2, 1, tzinfo=timezone.utc)
    t2 = datetime(2024, 3, 1, tzinfo=timezone.utc)
    t3 = datetime(2024, 4, 1, tzinfo=timezone.utc)
    digest_a = _sha(original)
    digest_b = _sha(b"%PDF-v2")
    quiet = reg.confirm_content(file_id, None, 99, t3, origin_batch)
    assert quiet.content_version is None
    assert quiet.nbytes == len(original)

    first_version = reg.confirm_content(file_id, digest_a, len(original), t1, origin_batch)
    assert first_version.file_id == file_id
    assert first_version.content_version == 1
    assert first_version.source_sha256 == digest_a
    assert first_version.batch_id == origin_batch
    assert first_version.created_at == created_at
    assert first_version.id_origin == "V7"

    same = reg.confirm_content(file_id, digest_a, len(original), t2, origin_batch)
    assert same.content_version == 1
    assert same.mtime == t2
    assert len(reg.content_versions(file_id)) == 1

    mismatch = reg.confirm_content(file_id, digest_a, len(original) + 1, t3, origin_batch)
    assert mismatch.content_version == 1
    assert mismatch.mtime == t2
    assert mismatch.nbytes == len(original)

    changed = reg.confirm_content(file_id, digest_b, len(b"%PDF-v2"), t3, rescanned[0].batch_id)
    assert changed.file_id == file_id
    assert changed.batch_id == origin_batch
    assert changed.content_version == 2
    assert changed.source_sha256 == digest_b
    assert changed.source_path == str(target)

    again = reg.confirm_content(file_id, digest_a, len(original), t3, origin_batch)
    assert again.file_id == file_id
    assert again.content_version == 3
    versions = reg.content_versions(file_id)
    assert [item.content_version for item in versions] == [1, 2, 3]
    assert [item.source_sha256 for item in versions] == [digest_a, digest_b, digest_a]
    assert versions[0].source_sha256 == digest_a
    assert versions[1].batch_id == rescanned[0].batch_id


def test_short_path_hash_is_rejected(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"%PDF-a")
    reg = Registry(factory=_factory())
    row = reg.scan(str(tmp_path), 10)[0]
    path_hash = hashlib.sha256(row.source_path.encode()).hexdigest()[:32]
    with pytest.raises(ContentRejected):
        reg.confirm_content(row.file_id, path_hash, row.nbytes, row.mtime, row.batch_id)
    assert row.source_sha256 is None
    assert row.content_version is None


def test_stat_failure_skips_and_records_without_failing_the_batch(tmp_path, monkeypatch):
    blocked = tmp_path / "a.pdf"
    later = tmp_path / "b.pdf"
    blocked.write_bytes(b"%PDF-a")
    later.write_bytes(b"%PDF-b")
    real_stat = os.stat

    def flaky(path, *args, **kwargs):
        if os.path.normpath(str(path)) == os.path.normpath(str(blocked)):
            raise OSError(2, "stat failed")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr("registry.paths.os.stat", flaky)
    reg = Registry(factory=_factory())
    rows = reg.scan(str(tmp_path), 10)
    assert [row.source_path for row in rows] == [str(later)]
    assert len(reg.stat_errors) == 1
    assert str(blocked) in reg.stat_errors[0]
    batch = reg.db.batch("scan-1")
    assert batch.scan_status == "COMPLETED"
    assert batch.skipped_count == 1
    assert batch.files_registered == 1
    assert batch.cursor_path == str(later)
    assert reg.db.get_by_path(str(blocked)) is None


def test_lstat_failure_does_not_advance_cursor(tmp_path, monkeypatch):
    early = tmp_path / "a.pdf"
    missing = tmp_path / "m.pdf"
    later = tmp_path / "z.pdf"
    early.write_bytes(b"%PDF-a")
    missing.write_bytes(b"%PDF-m")
    later.write_bytes(b"%PDF-z")
    real_lstat = os.lstat

    def flaky(path, *args, **kwargs):
        if os.path.normpath(str(path)) == os.path.normpath(str(missing)):
            raise OSError(2, "lstat failed")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr("registry.paths.os.lstat", flaky)
    reg = Registry(factory=_factory())
    rows = reg.scan(str(tmp_path), 10)
    assert [row.source_path for row in rows] == [str(early)]
    batch = reg.db.batch("scan-1")
    assert batch.scan_status == "FAILED"
    assert batch.cursor_path == str(early)
    assert batch.cursor_path != str(missing)
    assert reg.db.get_by_path(str(missing)) is None
    assert reg.db.get_by_path(str(later)) is None
    with pytest.raises(ScanBusy):
        reg.scan(str(tmp_path), 10)


def test_registry_sources_do_not_reference_redis():
    import registry.memory as memory
    import registry.paths as paths
    import registry.service as service
    import registry.uuidv7 as uuidv7

    for module in (memory, paths, service, uuidv7):
        assert "redis" not in inspect.getsource(module).lower()
