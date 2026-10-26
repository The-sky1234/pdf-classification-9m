import json
import os

os.environ.setdefault("API_KEY", "test-key")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app import build_app
from backend.models import ArchiveExecution, ProcessingBatch
from scheduler.phase1_loop import drain_all, redrive, serve
from scheduler.queue import FakeQueue

H = {"X-API-Key": "test-key"}


def test_release_lets_a_failed_task_enqueue_again():
    q = FakeQueue()
    assert q.enqueue("t1", {"n": 1}) is True
    assert q.enqueue("t1", {"n": 2}) is False
    assert q.dequeue()[0] == "t1"
    assert q.enqueue("t1", {"n": 3}) is False
    q.release("t1")
    assert q.enqueue("t1", {"n": 3}) is True
    assert q.qsize() == 1


def test_scan_drains_into_archive_and_review_replaces_it(tmp_path, monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("CLASSIFIER_BACKEND", "keyword")
    monkeypatch.delenv("THRESHOLDS_CALIBRATED", raising=False)
    src = tmp_path / "src"
    src.mkdir()
    (src / "notice.txt").write_text("abcdefghijklmnop 补充说明，正文里没有场景名。", encoding="utf-8")
    archive = tmp_path / "archive"
    app = build_app(f"sqlite:///{tmp_path}/loop.db")
    with TestClient(app) as client:
        scanned = client.post("/tasks/scan", json={"root": str(src)}, headers=H)
        assert scanned.status_code == 202
        body = scanned.json()
        assert app.state.pdf_queue.qsize() == 1
        db = app.state.sessions()
        batch = db.get(ProcessingBatch, body["batch_id"])
        assert json.loads(batch.source_roots) == [os.path.abspath(str(src))]
        assert redrive(db, app.state.pdf_queue, app.state.archive_queue)["pdf"] == 0
        steps = drain_all(db, app.state.pdf_queue, app.state.archive_queue, str(archive))
        assert any(step["kind"] == "archive" and step["status"] == "COMPLETED" for step in steps)
        fid = body["items"][0]["file_id"]
        held = client.get(f"/archive/{fid}", headers=H).json()
        assert held["archive_type"] == "PENDING"
        assert held["status"] == "COMPLETED"
        assert held["counts_as_business_archive"] is False
        assert "04_待复核暂存" in held["final_path"]
        assert os.path.isfile(held["final_path"])
        result_id = next(step["result_id"] for step in steps if step["kind"] == "pdf")
        reviewed = client.post(f"/review/{result_id}", json={
            "reviewer": "expert", "decision": "BUSINESS", "level1": "L1_AQSC",
            "level2": "消防监管", "expected_version": held["version"]}, headers=H)
        assert reviewed.status_code == 200
        assert reviewed.json()["to_version"] == held["version"] + 1
        assert app.state.archive_queue.qsize() == 1
        drain_all(db, app.state.pdf_queue, app.state.archive_queue, str(archive))
        final = client.get(f"/archive/{fid}", headers=H).json()
        assert final["archive_type"] == "BUSINESS"
        assert final["level2"] == "消防监管"
        assert final["counts_as_business_archive"] is True
        assert os.path.isfile(final["final_path"])
        statuses = db.scalars(select(ArchiveExecution.status).where(
            ArchiveExecution.file_id == fid)).all()
        assert "SUPERSEDED" in statuses and "COMPLETED" in statuses
        db.close()


def test_redrive_restores_a_dequeued_pending_task(tmp_path, monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("尚未处理", encoding="utf-8")
    app = build_app(f"sqlite:///{tmp_path}/redrive.db")
    with TestClient(app) as client:
        assert client.post("/tasks/scan", json={"root": str(src)}, headers=H).status_code == 202
        lost = app.state.pdf_queue.dequeue()
        assert lost and app.state.pdf_queue.qsize() == 0
        db = app.state.sessions()
        assert redrive(db, app.state.pdf_queue, app.state.archive_queue)["pdf"] == 1
        assert app.state.pdf_queue.qsize() == 1
        assert redrive(db, app.state.pdf_queue, app.state.archive_queue)["pdf"] == 0
        db.close()


def test_results_enqueue_archive_message(tmp_path, monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    app = build_app(f"sqlite:///{tmp_path}/results.db")
    with TestClient(app) as client:
        saved = client.post("/results", json={
            "file_path": "/x.pdf", "file_id": "fid-biz", "result_type": "CLASSIFIED",
            "level1": "L1_AQSC", "level2": "消防监管", "calibrated": True,
            "content_version": 1}, headers=H)
        assert saved.status_code == 201
        assert app.state.archive_queue.qsize() == 1
        key, payload = app.state.archive_queue.dequeue()
        assert key == "fid-biz:1"
        assert payload["content_version"] == 1


def test_decide_worker_stops_without_touching_redis(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/serve.db")
    monkeypatch.setenv("CLASSIFIER_BACKEND", "keyword")
    monkeypatch.delenv("REDIS_URL", raising=False)
    assert serve("decide", lambda: True) == 0


def test_archive_worker_requires_archive_root(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    monkeypatch.delenv("ARCHIVE_ROOT", raising=False)
    with pytest.raises(SystemExit):
        serve("archive", lambda: True)
