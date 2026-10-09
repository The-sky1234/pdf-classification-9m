"""ISSUE-07复现：未校准阈值不得自动归档；ISSUE-04：非CLASSIFIED禁带类别。"""
import os
os.environ["API_KEY"] = "test-key"
import pytest
from fastapi.testclient import TestClient
from backend.app import build_app
from classifier.pipeline import ClassificationPipeline
from classifier.config import Thresholds
from classifier.backends import KeywordEmbedding, KeywordReranker, ScriptedLLM


@pytest.fixture()
def client(tmp_path):
    with TestClient(build_app(f"sqlite:///{tmp_path}/g.db")) as c:
        yield c


def P(title="", paras=()):
    return {"file_path": "x.pdf", "pdf_type": "TEXT_PDF", "evidence_mode": "TITLE",
            "title": title, "headings": [],
            "pages": [{"page_no": i + 1, "text": t} for i, t in enumerate(paras)]}


def test_uncalibrated_blocks_auto_archive():
    pipe = ClassificationPipeline(KeywordEmbedding(), KeywordReranker(),
                                  ScriptedLLM(), Thresholds())
    assert not Thresholds().calibrated
    r = pipe.run(P("危险化学品重大危险源专项检查通知",
                   ["危险化学品企业重大危险源隐患排查，化工装置许可检查。"]))
    assert r["result_type"] == "MANUAL_REVIEW"
    assert r["reason"] == "uncalibrated_thresholds_blocked"


def test_calibrated_allows_direct():
    pipe = ClassificationPipeline(KeywordEmbedding(), KeywordReranker(),
                                  ScriptedLLM(), Thresholds(calibrated=True))
    r = pipe.run(P("危险化学品重大危险源专项检查通知",
                   ["危险化学品企业重大危险源隐患排查，化工装置许可检查。"]))
    assert r["result_type"] == "CLASSIFIED"


def test_non_classified_with_categories_rejected(client):
    H = {"X-API-Key": "test-key"}
    for rt in ("IRRELEVANT", "MANUAL_REVIEW"):
        r = client.post("/results", json={"file_path": "/x.pdf", "result_type": rt,
                                          "level1": "L1_AQSC", "level2": "L2_WHX"}, headers=H)
        assert r.status_code == 400, (rt, r.text)


def test_import_has_no_side_effects(tmp_path, monkeypatch):
    import subprocess, sys
    code = ("import sys; sys.path.insert(0, '.');"
            "import backend.app; import os;"
            "print('DEVDB_EXISTS' if os.path.exists('dev.db') else 'CLEAN')")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=str(tmp_path),
                         env={"PATH": os.environ["PATH"], "PYTHONPATH": os.path.abspath("."),
                              "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")})
    assert "CLEAN" in out.stdout, out.stderr[-500:]


def test_unresolved_saved_without_categories(client):
    H = {"X-API-Key": "test-key"}
    r = client.post("/results", json={"file_path": "/x.pdf", "result_type": "UNRESOLVED",
                                      "relevance_status": "UNCERTAIN",
                                      "suggested_action": "REPARSE_OR_VISION"}, headers=H)
    assert r.status_code == 201
    bad = client.post("/results", json={"file_path": "/x.pdf", "result_type": "UNRESOLVED",
                                        "level1": "L1_AQSC"}, headers=H)
    assert bad.status_code == 400
