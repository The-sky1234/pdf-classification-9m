import zipfile
from parser.documents import parse_document


def test_text_markup_and_office(tmp_path):
    txt = tmp_path / "a.txt"
    txt.write_text("第一行标题\n消防监管检查通知", encoding="utf-8")
    parsed = parse_document(str(txt))
    assert parsed["ok"] and parsed["doc_type"] == "text"
    assert "消防监管" in parsed["pages"][0]["text"]
    html = tmp_path / "a.html"
    html.write_text("<html><body><h1>纪检监察</h1><p>谈话记录</p></body></html>", encoding="utf-8")
    markup = parse_document(str(html))
    assert markup["doc_type"] == "markup" and "<" not in markup["pages"][0]["text"]
    assert "纪检监察" in markup["pages"][0]["text"]
    docx = tmp_path / "a.docx"
    xml = (
        '<?xml version="1.0"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>隐患排查台账</w:t></w:r></w:p></w:body></w:document>"
    )
    with zipfile.ZipFile(docx, "w") as zf:
        zf.writestr("word/document.xml", xml)
    office = parse_document(str(docx))
    assert office["doc_type"] == "docx" and office["pages"][0]["text"] == "隐患排查台账"


def test_failures_and_types_without_text(tmp_path):
    empty = tmp_path / "empty.md"
    empty.write_bytes(b"")
    assert parse_document(str(empty))["error"]["code"] == "ZERO_PAGES"
    bad = tmp_path / "bad.xlsx"
    bad.write_bytes(b"PK broken")
    assert parse_document(str(bad))["error"]["code"] == "CORRUPT_UNRECOVERABLE"
    image = parse_document(str(_touch(tmp_path / "a.png", b"\x89PNG")))
    assert image["ok"] and image["doc_type"] == "image" and image["pages"] == []
    ole = parse_document(str(_touch(tmp_path / "a.doc", b"ole")))
    assert ole["ok"] and ole["doc_type"] == "ole" and ole["error"] is None
    unknown = parse_document(str(_touch(tmp_path / "a.bin", b"abc")))
    assert unknown["ok"] is False and unknown["error"]["code"] == "UNSUPPORTED"
    assert unknown["error"]["retryable"] is False


def _touch(path, data: bytes):
    path.write_bytes(data)
    return path
