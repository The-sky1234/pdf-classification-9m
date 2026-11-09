"""波次 0 取字和规则的边界：编码、加密、损坏、扫描判定、专利误判。"""
import io
import zipfile

import pymupdf
import pytest

from phase1.extract import extract_cpu, rtf_to_text, strip_repeated_lines
from phase1.judge import judge_file
from phase1.rules import patent_status

BODY = "关于开展危险化学品重大危险源专项检查的通知，各单位要按要求组织排查整改。"


def _docx_bytes(text):
    buf = io.BytesIO()
    xml = ('<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>")
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", xml.encode("utf-8"))
    return buf.getvalue()


@pytest.mark.parametrize("codec", ["gb18030", "utf-16", "utf-8-sig", "utf-8"])
def test_plain_text_encodings(tmp_path, codec):
    path = tmp_path / "notice.txt"
    path.write_bytes(BODY.encode(codec))
    got = extract_cpu(str(path))
    assert got.outcome == "text" and "危险化学品" in got.text, (codec, got.text[:40])


def test_plain_text_reads_head_only(tmp_path):
    path = tmp_path / "big.log"
    path.write_bytes((BODY + "\n").encode("utf-8") * 200_000)
    got = extract_cpu(str(path))
    assert got.outcome == "text" and len(got.text) <= 8000


def test_rtf_chinese():
    raw = (rb"{\rtf1\ansi\ansicpg936{\fonttbl{\f0 \'cb\'ce\'cc\'e5;}}\f0 "
           rb"\'cf\'fb\'b7\'c0\'bc\'e0\'b6\'bd\par \u24212?\u24613?}")
    assert rtf_to_text(raw).strip() == "消防监督 应急"


def test_encrypted_office_is_pending_not_lv03(tmp_path):
    path = tmp_path / "secret.docx"
    path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 600)
    d = judge_file(str(path))
    assert d.archive_type == "PENDING" and d.reason == "encrypted"


def test_truncated_docx_is_recovered(tmp_path):
    blob = _docx_bytes(BODY)
    path = tmp_path / "cut.docx"
    path.write_bytes(blob[: blob.rfind(b"PK\x01\x02")])
    got = extract_cpu(str(path))
    assert got.outcome == "text" and got.error_code == "RECOVERED" and "危险化学品" in got.text


def test_broken_docx_is_lv03(tmp_path):
    path = tmp_path / "broken.docx"
    path.write_bytes(b"PK\x03\x04" + b"\x00" * 10 + b"garbage")
    d = judge_file(str(path))
    assert d.archive_type == "LOW_VALUE" and d.reason == "LV03"


def test_renamed_file_is_pending(tmp_path):
    path = tmp_path / "notpdf.pdf"
    path.write_bytes(b"<html>error page</html>")
    d = judge_file(str(path))
    assert d.archive_type == "PENDING" and d.reason == "format_mismatch"


def test_encrypted_pdf_is_pending(tmp_path):
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "secret")
    path = tmp_path / "enc.pdf"
    doc.save(str(path), encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o")
    d = judge_file(str(path))
    assert d.archive_type == "PENDING" and d.reason == "encrypted"


def test_corrupt_pdf_is_lv03(tmp_path):
    path = tmp_path / "bad.pdf"
    path.write_bytes(b"%PDF-1.4\n" + b"\x00garbage" * 50)
    d = judge_file(str(path))
    assert d.archive_type == "LOW_VALUE" and d.reason == "LV03"


def test_watermark_only_scan_goes_to_ocr(tmp_path):
    """每页只有一行相同水印的扫描件，去掉重复行以后才看得出没有正文。"""
    doc = pymupdf.open()
    for _ in range(4):
        page = doc.new_page()
        page.insert_text((72, 72), "Internal use only - department of emergency management archive copy")
    path = tmp_path / "scan.pdf"
    doc.save(str(path))
    got = extract_cpu(str(path))
    assert got.outcome == "needs_ocr" and got.ocr_pages == [0, 1, 2, 3]


def test_text_pdf_samples_first_pages_only(tmp_path):
    doc = pymupdf.open()
    for i in range(30):
        doc.new_page().insert_text((72, 72), f"Page {i} emergency rescue plan drill report " * 3)
    path = tmp_path / "long.pdf"
    doc.save(str(path))
    got = extract_cpu(str(path))
    assert got.outcome == "text" and got.page_count == 30 and "Page 29" not in got.text


def test_strip_repeated_lines_keeps_body():
    pages = ["机密\n第一页正文", "机密\n第二页正文", "机密\n第三页正文"]
    assert strip_repeated_lines(pages) == ["第一页正文", "第二页正文", "第三页正文"]


CNIPA = ("(19)中华人民共和国国家知识产权局\n(12)发明专利申请\n(10)申请公布号 CN 111234567 A\n"
         "(43)申请公布日 2020.05.01\n(21)申请号 201911234567.8\n(22)申请日 2019.12.01\n"
         "(71)申请人 某某科技有限公司\n(54)发明名称 一种矿用救援装置\n(57)摘要 本发明公开了……")


def test_patent_document_approved():
    assert patent_status("x.pdf", CNIPA, text_missing=False) == "approved"


def test_patent_certificate_approved():
    text = "发明专利证书\n发明名称：一种灭火装置\n专利号：ZL 2019 1 0123456.7\n国家知识产权局局长"
    assert patent_status("证书.pdf", text, text_missing=False) == "approved"


def test_business_doc_mentioning_patent_is_not_approved():
    """申报材料里写「已获得发明专利（专利号：ZL…）」不是专利文献。"""
    text = ("关于申报2024年度高新技术企业的请示\n市应急管理局：\n我单位近三年研发投入持续增长，"
            "已获得发明专利（专利号：ZL201910123456.7）等知识产权，现申请认定。")
    assert patent_status("申报请示.docx", text, text_missing=False) == "review"


def test_patent_mention_deep_in_body_passes():
    text = BODY * 30 + "附件：相关专利 ZL201910123456.7 的说明"
    assert patent_status("通知.docx", text, text_missing=False) == "pass"
    assert patent_status("通知.docx", BODY, text_missing=False) == "pass"


def test_patent_number_filename_with_header():
    assert patent_status("CN111234567A.pdf", "(21)申请号 201911234567.8 摘要……", text_missing=False) == "approved"
    assert patent_status("CN111234567A.pdf", BODY, text_missing=False) == "review"
