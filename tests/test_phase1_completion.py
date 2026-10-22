import os
import re
os.environ["API_KEY"] = "test-key"
import pymupdf
import pytest
from fastapi.testclient import TestClient
from backend.app import build_app
from backend.models import make_engine, make_session, init_db
from classifier.features import build
from exclusion.flow import decide
from exclusion.gates import check_low_value, check_patent
from registry.ids import uuid7
from scheduler.versions import message_is_current

H = {"X-API-Key": "test-key"}
V7 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("ARCHIVE_ROOT", raising=False)
    app = build_app(f"sqlite:///{tmp_path}/t.db")
    with TestClient(app) as c:
        yield c


def _pdf(path, text="Fire supervision inspection notice. Each unit inspects hazards and files a written rectification plan."):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    doc.save(path)
    doc.close()


def test_uuid7_shape():
    assert V7.match(uuid7())


def test_scan_file_id_stable_and_archive_root_rejected(client, tmp_path, monkeypatch):
    d = tmp_path / "pdfs"
    d.mkdir()
    (d / "a.pdf").write_bytes(b"%PDF-1.4 a")
    (d / "b.pdf").write_bytes(b"%PDF-1.4 b")
    first = client.post("/tasks/scan", json={"root": str(d), "limit": 1}, headers=H)
    assert first.status_code == 202
    body = first.json()
    assert body["accepted"] == 1 and V7.match(body["items"][0]["file_id"])
    assert body["items"][0]["file_id"] != body["items"][0]["task_id"]
    assert body["task_ids"] == [body["items"][0]["task_id"]]
    assert body["items"][0]["content_version"] is None
    again = client.post("/tasks/scan", json={"root": str(d), "batch_id": body["batch_id"]}, headers=H)
    assert again.status_code == 202
    ids = {it["file_id"] for it in again.json()["items"]}
    assert body["items"][0]["file_id"] not in ids
    full = client.post("/tasks/scan", json={"root": str(d)}, headers=H).json()
    assert body["items"][0]["file_id"] in {it["file_id"] for it in full["items"]}
    monkeypatch.setenv("ARCHIVE_ROOT", str(d))
    denied = client.post("/tasks/scan", json={"root": str(d)}, headers=H)
    assert denied.status_code == 400


def test_taxonomy_and_results_and_archive(client):
    tax = client.get("/taxonomy", headers=H).json()
    assert tax["total_l2"] == 27 and tax["taxonomy_version"] == "v3.0-phase1"
    assert tax["l2_counts"]["L1_ZHGL"] == 11
    legacy = client.post("/results", json={"file_path": "/x.pdf", "file_id": "fid-old",
                                           "result_type": "CLASSIFIED", "level1": "L1_AQSC",
                                           "level2": "L2_WHX", "file_hash": "ab" * 32}, headers=H)
    assert legacy.status_code == 201
    assert client.get("/archive/fid-old", headers=H).status_code == 404
    again = client.post("/results", json={"file_path": "/x.pdf", "file_id": "fid-old",
                                          "result_type": "CLASSIFIED", "level1": "L1_AQSC",
                                          "level2": "L2_WHX"}, headers=H)
    assert again.json()["id"] == legacy.json()["id"]
    bad = client.post("/results", json={"file_path": "/x.pdf", "result_type": "CLASSIFIED",
                                        "level1": "L1_AQSC", "level2": "纪检监察"}, headers=H)
    assert bad.status_code == 400
    irr = client.post("/results", json={"file_path": "/x.pdf", "file_id": "fid-irr",
                                        "result_type": "IRRELEVANT"}, headers=H)
    assert irr.status_code == 201
    assert client.get("/archive/fid-irr", headers=H).status_code == 404
    biz = client.post("/results", json={"file_path": "/x.pdf", "file_id": "fid-biz",
                                        "result_type": "CLASSIFIED", "level1": "L1_AQSC",
                                        "level2": "消防监管", "calibrated": True,
                                        "content_version": 1}, headers=H)
    assert biz.status_code == 201
    view = client.get("/archive/fid-biz", headers=H).json()
    assert view["target_dir"].endswith("消防监管/")
    assert view["status"] == "QUEUED" and view["counts_as_business_archive"] is False
    assert view["content_version"] == 1 and view["version"] == 1
    detail = client.get(f"/results/{biz.json()['id']}", headers=H).json()
    assert detail["file_id"] == "fid-biz" and detail["checksum_verified"] is False
    unresolved = client.post("/results", json={"file_path": "/u.pdf", "result_type": "UNRESOLVED"},
                             headers=H).json()
    queue = client.get("/review/queue", headers=H).json()
    assert any(i["id"] == unresolved["id"] and i["reason"] == "UNRESOLVED" for i in queue["items"])
    reviewed = client.post(f"/review/{unresolved['id']}", json={
        "reviewer": "expert", "decision": "BUSINESS", "level1": "L1_AQSC", "level2": "消防监管",
        "expected_version": 0}, headers=H)
    assert reviewed.status_code == 200 and reviewed.json()["to_version"] == 1
    clash = client.post(f"/review/{unresolved['id']}", json={
        "reviewer": "expert", "decision": "PATENT", "expected_version": 0}, headers=H)
    assert clash.status_code == 409


def test_gates_do_not_guess():
    patent = decide("f", {"ok": True, "file_path": "CN112233445A.pdf", "pdf_type": "TEXT_PDF",
                          "title": "发明专利说明书", "headings": [], "evidence_mode": "TITLE",
                          "pages": [{"page_no": 1, "text": "申请公布号 CN112233445A 本发明涉及一种救援装备。"}]})
    assert patent["archive_type"] == "PATENT" and patent["target_dir"] == "01_专利文件/"
    unsure = check_patent("f", {"filename": "关于专利工作的通知.pdf", "title": "关于专利工作的通知",
                                "query_text": "请各单位学习专利法规。"})
    assert unsure["status"] == "NEED_REVIEW"
    missing = decide("f", {"ok": True, "file_path": "scan.pdf", "pdf_type": "SCAN_PDF",
                           "title": "", "pages": [], "evidence_mode": "OCR_BODY"})
    assert missing["archive_type"] == "PENDING"
    assert decide("f", {"ok": False, "error": {"code": "OCR_FAILED", "retryable": True}})["action"] == "RETRY"
    broken = decide("f", {"ok": False, "error": {"code": "CORRUPT_UNRECOVERABLE", "retryable": False}})
    assert broken["reason"] == "LV03" and broken["target_dir"].endswith("无效文件/")
    assert check_low_value("f", {"query_text": "只有几个字"}, {"is_exact_dup": False})["status"] == "PASS"
    dup = check_low_value("f", {"query_text": "正文足够"}, {"is_exact_dup": True, "master_is_patent": False})
    assert dup["reason"] == "LV02"
    copy = check_low_value("f", {"query_text": "正文足够"}, {"is_exact_dup": True, "master_is_patent": True})
    assert copy["status"] == "NEED_REVIEW"
    long = "危险化学品监管" * 2000
    feat = build({"ok": True, "title": "危险化学品监管通知", "headings": [], "evidence_mode": "BODY",
                  "pdf_type": "TEXT_PDF", "pages": [{"page_no": 1, "text": long}]})
    assert len(feat["query_text"]) <= 3000 and feat["page_evidence"]
    assert message_is_current(None, None) and not message_is_current(1, 2)
    assert message_is_current(2, 2)


def test_worker_copies_verified_pdf(tmp_path):
    from registry.register import scan_register
    from scheduler.phase1_worker import handle_archive_task, handle_pdf_task
    src = tmp_path / "src"
    src.mkdir()
    _pdf(str(src / "notice.pdf"))
    engine = make_engine(f"sqlite:///{tmp_path}/w.db")
    init_db(engine)
    db = make_session(engine)()
    scanned = scan_register(db, str(src), 10)
    msg = scanned["items"][0]

    def classify_fn(_parse):
        return {"result_type": "CLASSIFIED", "level1": "L1_AQSC", "level2": "消防监管",
                "calibrated": True}

    decided = handle_pdf_task(db, msg, classify_fn=classify_fn)
    assert decided["action"] == "DECIDE"
    stale = dict(decided["queue"], content_version=0)
    # 源已经是版本 1，更旧的归档消息不能完成。
    from backend import store
    reg_version = store.archive_view(db, msg["file_id"])["content_version"]
    assert reg_version == 1
    dropped = handle_archive_task(db, stale, str(tmp_path / "archive"))
    assert dropped["action"] == "DROP"
    done = handle_archive_task(db, decided["queue"], str(tmp_path / "archive"))
    assert done["status"] == "COMPLETED"
    view = store.archive_view(db, msg["file_id"])
    assert view["counts_as_business_archive"] is True
    assert view["checksum_verified"] is True
    assert os.path.isfile(view["final_path"])
    again = handle_archive_task(db, decided["queue"], str(tmp_path / "archive"))
    assert again["final_path"] == done["final_path"]
