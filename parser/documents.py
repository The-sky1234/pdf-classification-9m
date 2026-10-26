from __future__ import annotations
import os
import re
import zipfile
from xml.etree import ElementTree as ET

from parser.parser import parse_pdf

_W_T = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"
_W_P = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"
_A_T = "{http://schemas.openxmlformats.org/drawingml/2006/main}t"

TEXT_EXT = {".txt", ".md", ".log", ".csv", ".json"}
HTML_EXT = {".html", ".htm", ".xml", ".rtf"}
DOCX_EXT = {".docx", ".docm"}
XLSX_EXT = {".xlsx", ".xlsm"}
PPTX_EXT = {".pptx", ".pptm"}
OFD_EXT = {".ofd"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp"}
OLE_EXT = {".doc", ".wps", ".xls", ".et", ".ppt", ".dps"}
PDF_EXT = {".pdf"}


def parse_document(file_path: str, ocr_backend=None, mineru=None, max_pages: int = 0):
    """按扩展名抽取关键文本。PDF 仍走原解析；去重不在这里做。"""
    ext = os.path.splitext(file_path)[1].lower()
    try:
        size = os.path.getsize(file_path)
    except OSError as e:
        return _fail(file_path, "RETRYABLE_IO", str(e), True, ext)
    if size == 0:
        return _fail(file_path, "ZERO_PAGES", "empty file", False, ext)
    if ext in PDF_EXT:
        out = parse_pdf(file_path, ocr_backend=ocr_backend, mineru=mineru, max_pages=max_pages).to_dict()
        out["doc_type"] = "pdf"
        return out
    if ext in TEXT_EXT:
        return _text_doc(file_path, _read_text(file_path), "text")
    if ext in HTML_EXT:
        raw = _read_text(file_path)
        if ext == ".rtf":
            raw = re.sub(r"\\[a-z]+\d* ?", " ", raw)
            raw = re.sub(r"[{}]", " ", raw)
        else:
            raw = re.sub(r"<[^>]+>", " ", raw)
        return _text_doc(file_path, re.sub(r"\s+", " ", raw).strip(), "markup")
    if ext in DOCX_EXT | XLSX_EXT | PPTX_EXT | OFD_EXT:
        try:
            text = _office_text(file_path, ext)
        except (zipfile.BadZipFile, KeyError, ET.ParseError) as e:
            return _fail(file_path, "CORRUPT_UNRECOVERABLE", str(e), False, ext.lstrip("."))
        return _text_doc(file_path, text, ext.lstrip("."))
    if ext in IMAGE_EXT:
        return _image_doc(file_path, ocr_backend)
    if ext in OLE_EXT:
        return _text_doc(file_path, "", "ole")
    return _fail(file_path, "UNSUPPORTED", f"unsupported type {ext or 'none'}", False, ext.lstrip("."))


def _read_text(path: str) -> str:
    data = open(path, "rb").read()
    return data.decode("utf-8", "replace")


def _office_text(path: str, ext: str) -> str:
    with zipfile.ZipFile(path) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".xml")]
        if ext in DOCX_EXT:
            names = [n for n in names if n.startswith("word/")] or names
        elif ext in PPTX_EXT:
            names = [n for n in names if n.startswith("ppt/slides/")] or names
        elif ext in XLSX_EXT:
            names = [n for n in names if "sharedStrings" in n or n.startswith("xl/worksheets/")] or names
        chunks = []
        for name in names:
            chunks.extend(_xml_text(zf.read(name)))
    return "\n".join(chunks)


def _xml_text(blob: bytes) -> list[str]:
    root = ET.fromstring(blob)
    lines = []
    paragraphs = [p for p in root.iter(_W_P)]
    if paragraphs:
        for p in paragraphs:
            line = "".join((t.text or "") for t in p.iter(_W_T)).strip()
            if line:
                lines.append(line)
        return lines
    drawing = [t.text.strip() for t in root.iter(_A_T) if t.text and t.text.strip()]
    if drawing:
        return drawing
    return [t.text.strip() for t in root.iter() if t.text and t.text.strip()]


def _image_doc(path: str, ocr_backend) -> dict:
    if ocr_backend is None:
        return _text_doc(path, "", "image", pdf_type="IMAGE_PDF")
    text_parts = []
    try:
        lines = ocr_backend(open(path, "rb").read())
        text_parts = [item[0] for item in lines if item and item[0]]
    except Exception as e:
        return _fail(path, "OCR_FAILED", str(e), True, "image")
    return _text_doc(path, "\n".join(text_parts), "image", pdf_type="IMAGE_PDF")


def _text_doc(path: str, text: str, doc_type: str, pdf_type: str = "TEXT_PDF") -> dict:
    title = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    return {"file_path": path, "pdf_type": pdf_type, "doc_type": doc_type,
            "evidence_mode": "BODY" if text.strip() else "OCR_BODY",
            "page_count": 1 if text.strip() or pdf_type == "IMAGE_PDF" else 0,
            "title": title[:200], "headings": [],
            "pages": [{"page_no": 1, "text": text[:8000], "ocr_score": None}] if text.strip() else [],
            "ocr_mean_score": None, "ok": True, "error": None, "backend": doc_type,
            "truncated": len(text) > 8000}


def parse_for_role(file_path: str, role: str = "parser", ocr_backend=None, mineru=None,
                   max_pages: int = 0) -> dict:
    """按角色解析任意已登记类型。只有 PDF 才走 PyMuPDF、OCR 或 MinerU。"""
    ext = os.path.splitext(file_path)[1].lower()
    if ext in PDF_EXT:
        from parser.parser import parse_pdf
        parsed = parse_pdf(
            file_path,
            ocr_backend=ocr_backend if role == "ocr" else None,
            mineru=mineru if role == "mineru" else None,
            max_pages=max_pages,
        ).to_dict()
        parsed["doc_type"] = parsed.get("doc_type") or "pdf"
        return parsed
    if ext in IMAGE_EXT and role == "ocr":
        return parse_document(file_path, ocr_backend=ocr_backend, max_pages=max_pages)
    return parse_document(file_path, max_pages=max_pages)


def _fail(path: str, code: str, message: str, retryable: bool, doc_type: str) -> dict:
    return {"file_path": path, "pdf_type": None, "doc_type": doc_type, "evidence_mode": None,
            "page_count": 0, "title": "", "headings": [], "pages": [], "ok": False,
            "error": {"code": code, "message": message, "retryable": retryable},
            "backend": doc_type, "truncated": False}
