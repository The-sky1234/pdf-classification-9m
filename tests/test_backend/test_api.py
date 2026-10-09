import os
import pytest
os.environ["API_KEY"] = "test-key"
from fastapi.testclient import TestClient
from backend.app import build_app

H = {"X-API-Key": "test-key"}


@pytest.fixture()
def client(tmp_path):
    app = build_app(f"sqlite:///{tmp_path}/t.db")
    with TestClient(app) as c:
        yield c


def test_scan_idempotent(client, tmp_path):
    d = tmp_path / "pdfs"
    d.mkdir()
    (d / "a.pdf").write_bytes(b"%PDF-1.4 fake")
    r1 = client.post("/tasks/scan", json={"root": str(d)}, headers=H)
    r2 = client.post("/tasks/scan", json={"root": str(d)}, headers=H)
    assert r1.status_code == 202
    assert r1.json()["task_ids"] == r2.json()["task_ids"]


def test_task_and_result_flow(client):
    tid = client.post("/tasks/scan", json={"root": "."}, headers=H).json()
    payload = {"task_id": "t1", "file_path": "/x/a.pdf", "file_hash": "h" * 32,
               "result_type": "CLASSIFIED", "relevance_status": "RELEVANT",
               "evidence_mode": "TITLE", "level1": "L1_AQSC", "level2": "L2_WHX",
               "candidate_scores": {}, "decision_source": "BGE_DIRECT",
               "classification_basis": {}, "model_version": "m",
               "taxonomy_version": "v1.0-planner"}
    r = client.post("/results", json=payload, headers=H)
    assert r.status_code == 201
    rid = r.json()["id"]
    q = client.get("/results", headers=H).json()
    assert q["total"] >= 1 and q["items"]
    det = client.get(f"/results/{rid}", headers=H).json()
    assert det["level2"] == "L2_WHX"


def test_validation_and_auth(client):
    assert client.get("/results").status_code in (401, 422)
    bad = {"result_type": "NOPE", "file_path": "x"}
    r = client.post("/results", json=bad, headers=H)
    assert r.status_code == 400
    r = client.get("/results", params={"page": 1, "size": 5}, headers=H).json()
    assert r["page"] == 1 and len(r["items"]) <= 5


def test_review_flow(client):
    p = {"task_id": "t2", "file_path": "/x/b.pdf", "file_hash": "g" * 32,
         "result_type": "MANUAL_REVIEW", "evidence_mode": "BODY"}
    rid = client.post("/results", json=p, headers=H).json()["id"]
    q = client.get("/review/queue", headers=H).json()
    assert any(i["id"] == rid for i in q["items"])
    ok = client.post(f"/review/{rid}", json={"reviewer": "expert",
                                             "level1": "L1_FZZJ", "level2": "L2_FXKH"}, headers=H)
    assert ok.status_code == 200
