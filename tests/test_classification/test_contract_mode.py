"""ISSUE-03复现：evidence_mode必须取data-contract四值。"""
from classifier.evidence import route_evidence
from classifier.config import Thresholds

CONTRACT_MODES = {"TITLE", "BODY", "OCR_BODY", "VISION"}


def P(title="", paras=(), pdf_type="TEXT_PDF", mode="TITLE"):
    return {"file_path": "x.pdf", "pdf_type": pdf_type, "evidence_mode": mode,
            "title": title, "headings": [],
            "pages": [{"page_no": i + 1, "text": t} for i, t in enumerate(paras)]}


def test_modes_match_contract():
    th = Thresholds()
    cases = [
        P("安全生产检查通知", ["安全生产检查内容详述。"]),
        P("", ["正文段落内容详述第一段。" * 3]),
        P("", ["x"], pdf_type="SCAN_PDF", mode="OCR_BODY"),
        P("", [], pdf_type="IMAGE_PDF", mode="VISION"),
    ]
    for c in cases:
        assert route_evidence(c, th)["mode"] in CONTRACT_MODES
