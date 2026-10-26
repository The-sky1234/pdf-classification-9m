from conftest import make_text_pdf, make_blank_image_pdf, fake_ocr_backend
from parser.parser import parse_pdf, detect_only
from parser.detector import decide_type
from parser.router import route


def test_text_pdf_extract(tmp_path):
    p = str(tmp_path / "t.pdf")
    make_text_pdf(p, title="Hazardous Chemical Inspection Notice", body="Major hazard special inspection.")
    r = parse_pdf(p)
    assert r.ok and r.pdf_type == "TEXT_PDF" and "Hazardous" in r.title
    assert r.pages and r.pages[0].spans[0].bbox.x1 > 0


def test_scan_pdf_ocr_mock(tmp_path):
    p = str(tmp_path / "s.pdf")
    make_blank_image_pdf(p)
    r = parse_pdf(p, ocr_backend=fake_ocr_backend())
    assert r.ok and r.pdf_type in ("SCAN_PDF", "IMAGE_PDF", "MIXED_PDF")
    assert r.evidence_mode == "OCR_BODY" and r.ocr_mean_score and r.ocr_mean_score > 0.9


def test_no_title_keeps_body(tmp_path):
    from parser.parser import _open_readonly
    from parser import pymupdf_extractor as ex
    p = str(tmp_path / "n.pdf")
    make_text_pdf(p, title="   ", body="Body only without title here. " * 10)
    r = parse_pdf(p)
    assert r.ok and r.evidence_mode == "BODY"
    assert any(len(pg.text) > 0 for pg in r.pages)


def test_complex_routing_and_mineru_policy(tmp_path):
    assert route("COMPLEX_PDF", True)["backend"] == "mineru-on-demand"
    p = str(tmp_path / "t.pdf")
    make_text_pdf(p)
    r = parse_pdf(p, force_mineru=True)
    assert not r.ok and r.error.code == "MINERU_FAILED"


def test_corrupt_returns_structured_error(tmp_path):
    p = str(tmp_path / "bad.pdf")
    with open(p, "wb") as f:
        f.write(b"%PDF-1.4 broken {{{")
    r = parse_pdf(p)
    assert not r.ok and r.error is not None and r.error.code
    d = r.to_dict()
    assert set(d) >= {"file_path", "pdf_type", "evidence_mode", "page_count", "title",
                       "pages", "ok", "error", "backend"}


def test_contract_conformance(tmp_path):
    p = str(tmp_path / "t.pdf")
    make_text_pdf(p)
    d = parse_pdf(p).to_dict()
    assert isinstance(d["pages"][0]["spans"][0]["bbox"], dict)
    assert d["page_count"] == 1


def test_readonly_original_untouched(tmp_path):
    import hashlib
    p = str(tmp_path / "t.pdf")
    make_text_pdf(p)
    h0 = hashlib.md5(open(p, "rb").read()).hexdigest()
    parse_pdf(p, ocr_backend=fake_ocr_backend())
    detect_only(p)
    assert hashlib.md5(open(p, "rb").read()).hexdigest() == h0


def test_only_the_first_five_pages_are_read(tmp_path):
    import pymupdf as fitz
    p = str(tmp_path / "long.pdf")
    doc = fitz.open()
    for index in range(1, 7):
        page = doc.new_page()
        page.insert_text((72, 100), f"Safety inspection record page {index} hazard notice", fontsize=12)
    doc.save(p)
    doc.close()
    called = {"n": 0}

    def _ocr(_image):
        called["n"] += 1
        return [("should-not-ocr-text", (0, 0, 1, 1), 0.5)]

    parsed = parse_pdf(p, ocr_backend=_ocr)
    assert parsed.ok and parsed.truncated and parsed.page_count == 6
    assert len(parsed.pages) == 5
    joined = "\n".join(page.text for page in parsed.pages)
    assert "page 5" in joined and "page 6" not in joined
    assert called["n"] == 0
    assert parsed.evidence_mode != "OCR_BODY"


def test_image_pages_are_ocr_and_text_pages_are_not(tmp_path):
    import pymupdf as fitz
    p = str(tmp_path / "mixed.pdf")
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Fire supervision inspection notice for every unit.", fontsize=12)
    image_page = doc.new_page()
    blank = fitz.open()
    blank.new_page()
    image_page.insert_image(image_page.rect, stream=blank[0].get_pixmap(dpi=72).tobytes("png"))
    for index in range(3, 7):
        tail = doc.new_page()
        tail.insert_text((72, 100), f"Written page {index} stays as text and is not a picture.", fontsize=12)
    doc.save(p)
    doc.close()
    blank.close()
    seen = []

    def _ocr(_image):
        seen.append(1)
        return [("scanned fire supervision page", (10, 10, 80, 30), 0.91)]

    parsed = parse_pdf(p, ocr_backend=_ocr)
    assert parsed.ok and len(parsed.pages) == 5 and len(seen) == 1
    assert "Fire supervision" in parsed.pages[0].text
    assert parsed.pages[1].text == "scanned fire supervision page"
    assert parsed.evidence_mode == "OCR_BODY"


def test_vision_reserved():
    from parser.vision_stub import VisionStub
    import pytest
    with pytest.raises(NotImplementedError):
        VisionStub().describe(b"x")
