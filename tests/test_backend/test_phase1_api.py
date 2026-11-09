import os
import subprocess
import sys

import pytest

os.environ["API_KEY"] = "test-key"
from fastapi.testclient import TestClient

from backend.app import build_app
from phase1 import ledger

H = {"X-API-Key": "test-key"}
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def client(tmp_path):
    app = build_app(f"sqlite:///{tmp_path}/api.db")
    with TestClient(app) as c:
        yield c, app


def test_health_and_metrics_without_key(client):
    c, _ = client
    assert c.get("/healthz").json()["ok"] is True
    body = c.get("/metrics").text
    assert "# TYPE phase1_files gauge" in body
    assert c.get("/phase1/summary").status_code == 401


def test_taxonomy_is_v3(client):
    c, _ = client
    data = c.get("/taxonomy", headers=H).json()
    assert data["taxonomy_version"] == "v3" and sum(data["l2_counts"].values()) == 27


def test_phase1_review_endpoint(client, tmp_path):
    c, app = client
    engine = app.state.engine
    src = tmp_path / "a.txt"
    src.write_text("x", encoding="utf-8")
    ledger.load_masters(engine, [{"source_path": str(src)}])
    row = ledger.list_rows(engine)["items"][0]
    r = c.post(f"/phase1/files/{row['id']}/review", headers=H,
               json={"reviewer": "张三", "archive_type": "BUSINESS", "level1": "L1_ZHGL", "level2": "综合办公"})
    assert r.status_code == 409
    with engine.begin() as conn:
        conn.execute(ledger.files.update().values(stage="done", archive_type="PENDING", archive_path="/r/x.txt",
                                                  archive_sha256="a" * 64, archive_bytes=1))
    bad = c.post(f"/phase1/files/{row['id']}/review", headers=H,
                 json={"reviewer": "张三", "archive_type": "BUSINESS", "level1": "L1_AQSC", "level2": "综合办公"})
    assert bad.status_code == 400
    ok = c.post(f"/phase1/files/{row['id']}/review", headers=H,
                json={"reviewer": "张三", "archive_type": "LOW_VALUE", "reason": "LV01"})
    assert ok.status_code == 200 and ok.json()["stage"] == "archive"
    assert ok.json()["target_dir"] == "02_低价值文件/内容不足/"
    assert c.get("/phase1/files", params={"stage": "archive"}, headers=H).json()["total"] == 1
    assert c.get("/phase1/files/999", headers=H).status_code == 404
    assert c.get("/phase1/summary", headers=H).json()["by_stage"] == {"archive": 1}


def test_legacy_review_validates_categories(client):
    c, _ = client
    p = {"task_id": "t2", "file_path": "/x/b.pdf", "result_type": "MANUAL_REVIEW", "reason": "uncalibrated"}
    rid = c.post("/results", json=p, headers=H).json()["id"]
    assert c.get(f"/results/{rid}", headers=H).json()["reason"] == "uncalibrated"
    bad = c.post(f"/review/{rid}", json={"reviewer": "x", "level1": "L1_AQSC", "level2": "L2_FXKH"}, headers=H)
    assert bad.status_code == 400


def test_api_key_from_dotenv_and_other_cwd(tmp_path):
    """API_KEY 只写在 .env 里也要生效；从别的目录启动时 /taxonomy 不能 500。"""
    code = (
        "import os; os.environ.pop('API_KEY', None);"
        "import backend.app as a;"
        "from fastapi.testclient import TestClient;"
        f"c = TestClient(a.build_app('sqlite:///{tmp_path}/k.db'));"
        "print(c.get('/taxonomy', headers={'X-API-Key': 'from-dotenv'}).status_code,"
        "      c.get('/taxonomy', headers={'X-API-Key': 'wrong'}).status_code)"
    )
    env = {k: v for k, v in os.environ.items() if k != "API_KEY"}
    env["PYTHONPATH"] = ROOT
    dotenv = os.path.join(ROOT, ".env")
    if os.path.exists(dotenv):
        pytest.skip("仓库根已有 .env，不覆盖")
    with open(dotenv, "w", encoding="utf-8") as handle:
        handle.write("API_KEY=from-dotenv\n")
    try:
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(tmp_path), env=env)
    finally:
        os.remove(dotenv)
    assert out.stdout.split() == ["200", "401"], out.stderr[-800:]


def test_unset_api_key_refuses(tmp_path, monkeypatch):
    monkeypatch.delenv("API_KEY", raising=False)
    with TestClient(build_app(f"sqlite:///{tmp_path}/n.db")) as c:
        assert c.get("/taxonomy", headers={"X-API-Key": ""}).status_code == 503
        assert c.get("/healthz").json()["api_key_configured"] is False
