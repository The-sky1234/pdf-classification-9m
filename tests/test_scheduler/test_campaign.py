import json
import os
import zipfile

os.environ.setdefault("API_KEY", "test-key")

import pymupdf
from fastapi.testclient import TestClient

from backend.app import build_app
from backend.models import init_db, make_engine, make_session
from backend.store import metrics_text
from parser.documents import parse_for_role
from scheduler.campaign import PHASES, run_campaign

H = {"X-API-Key": "test-key"}
NOTICE = "消防监管检查通知。各单位排查隐患并形成书面整改台账，五日内反馈责任人、复查时间和逾期未报的督办记录。"


def test_parse_role_keeps_office_off_the_pdf_parser(tmp_path):
    path = tmp_path / "brief.docx"
    _docx(path, NOTICE)
    parsed = parse_for_role(str(path), "parser")
    assert parsed["ok"] and parsed["doc_type"] == "docx"
    assert "消防监管" in parsed["pages"][0]["text"]


def test_campaign_runs_every_phase_on_mixed_files(tmp_path, monkeypatch):
    monkeypatch.setenv("DEDUP_ALREADY_DONE", "1")
    monkeypatch.delenv("THRESHOLDS_CALIBRATED", raising=False)
    src = tmp_path / "src"
    src.mkdir()
    (src / "notice.txt").write_text(NOTICE, encoding="utf-8")
    (src / "page.html").write_text(f"<p>{NOTICE}</p>", encoding="utf-8")
    _docx(src / "brief.docx", NOTICE)
    _zip_xml(src / "sheet.xlsx", "xl/sharedStrings.xml", f"<sst><t>{NOTICE}</t></sst>")
    _zip_xml(src / "deck.pptx", "ppt/slides/slide1.xml",
             f'<p xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:t>{NOTICE}</a:t></p>')
    _zip_xml(src / "form.ofd", "Doc_0/Pages/Content.xml", f"<Page>{NOTICE}</Page>")
    (src / "scan.png").write_bytes(b"\x89PNG\r\n\x1a\nnot-a-real-image")
    (src / "empty.csv").write_bytes(b"")
    _pdf(src / "law.pdf", NOTICE)
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps([
        {"name": "notice.txt", "level2": "消防监管"},
        {"name": "empty.csv", "reason": "LV03"},
    ], ensure_ascii=False), encoding="utf-8")
    db = _session(tmp_path)
    report = run_campaign(db, str(src), str(tmp_path / "archive"), labels_path=str(labels))
    phases = report["phases"]
    assert list(phases) == list(PHASES)
    assert phases["1_knowledge"]["total_l2"] == 27 and phases["1_knowledge"]["ready"]
    assert phases["2_validate"]["passed"]
    assert set(phases["2_validate"]["kinds_seen"]) == {
        "txt", "html", "docx", "xlsx", "pptx", "ofd", "png", "csv", "pdf"}
    assert phases["3_calibrate"]["status"] == "scored"
    assert phases["3_calibrate"]["correct"] == 2
    assert phases["3_calibrate"]["thresholds_applied"] is False
    assert phases["4_stress"]["sample_files"] == 9
    assert "est_days" in phases["4_stress"]["estimate"]
    assert phases["5_trial"]["completed"] == 9
    assert phases["6_full"]["batch_id"]
    text = metrics_text(db)
    assert 'pdfcls_files_registered{ext="ofd"} 1' in text
    assert 'pdfcls_files_registered{ext="docx"} 1' in text
    assert 'pdfcls_archive_files{status="COMPLETED",archive_type="LOW_VALUE"}' in text
    archived = []
    for name in ("notice.txt", "page.html", "brief.docx", "sheet.xlsx", "deck.pptx",
                 "form.ofd", "scan.png", "empty.csv", "law.pdf"):
        matches = list((tmp_path / "archive").rglob(f"*{name}"))
        assert matches, name
        archived.append(matches[0].name)
    assert any(item.endswith(".ofd") for item in archived)
    assert any(item.endswith(".png") for item in archived)
    db.close()


def test_metrics_without_a_key(tmp_path, monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    app = build_app(f"sqlite:///{tmp_path}/m.db")
    with TestClient(app) as client:
        body = client.get("/metrics")
        assert body.status_code == 200
        assert "pdfcls_files_registered" in body.text
        denied = client.get("/taxonomy")
        assert denied.status_code == 401


def _session(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/campaign.db")
    init_db(engine)
    return make_session(engine)()


def _docx(path, text):
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
    )
    _zip_xml(path, "word/document.xml", xml)


def _zip_xml(path, name, xml):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(name, xml)


def _pdf(path, text):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    doc.save(path)
    doc.close()
