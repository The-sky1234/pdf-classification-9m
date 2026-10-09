"""QA复检：真实接口交接（parser→handoff→classifier→backend.store）与回写认证。"""
import os
os.environ["API_KEY"] = "test-key"
import pymupdf as fitz
from parser.parser import parse_pdf
from scheduler.handoff import FilePayloadStore
from classifier.pipeline import ClassificationPipeline
from classifier.config import Thresholds
from classifier.backends import KeywordEmbedding, KeywordReranker, ScriptedLLM
from backend.models import make_engine, make_session, init_db
from backend import store as bstore


def test_handoff_to_backend_store_real_interfaces(tmp_path):
    pdf = str(tmp_path / "q.pdf")
    doc = fitz.open()
    pg = doc.new_page()
    pg.insert_text((72, 100), "Flood Control Notice", fontsize=22)
    pg.insert_text((72, 160), "River flood prevention and drought relief deployment plan.",
                   fontsize=11)
    doc.save(pdf)
    doc.close()
    parsed = parse_pdf(pdf)
    assert parsed.ok
    h = FilePayloadStore(str(tmp_path / "pl"))
    ref = h.save("qa-1", "PARSED", parsed.to_dict())
    assert len(ref) < 2000
    clf = ClassificationPipeline(KeywordEmbedding(), KeywordReranker(),
                                 ScriptedLLM(), Thresholds(calibrated=True))
    out = clf.run(h.load(ref))
    eng = make_engine(f"sqlite:///{tmp_path}/qa.db")
    init_db(eng)
    db = make_session(eng)()
    row = bstore.save_result(db, {"task_id": "qa-1", "file_path": pdf,
                                  "file_hash": "a" * 32, **out})
    assert row.id and row.result_type == out["result_type"]
    total, _ = bstore.query_results(db)
    assert total == 1
    db.close()


def test_result_write_requires_auth(tmp_path):
    from fastapi.testclient import TestClient
    from backend.app import build_app
    with TestClient(build_app(f"sqlite:///{tmp_path}/a.db")) as c:
        payload = {"file_path": "/x.pdf", "result_type": "MANUAL_REVIEW"}
        assert c.post("/results", json=payload).status_code == 401
        ok = c.post("/results", json=payload, headers={"X-API-Key": "test-key"})
        assert ok.status_code == 201
