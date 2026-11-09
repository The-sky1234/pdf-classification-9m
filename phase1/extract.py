"""波次 0 的取字。只在 CPU 上做，不加载 OCR、MinerU 或分类模型。"""
from __future__ import annotations

import os
import re
import zipfile
from dataclasses import dataclass
from xml.etree import ElementTree as ET

from phase1.formats import CLOSED, IMAGE, NO_EXTRACTOR, OOXML, PDF, PLAIN, extension_of

_XML_TEXT = (
    "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t",
    "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t",
    "{http://schemas.openxmlformats.org/drawingml/2006/main}t",
)
# 少于这些字就当成没有可用文字层，交给 OCR 波次。不据此判内容不足。
_MIN_TEXT = 20


@dataclass
class Extract:
    path: str
    extension: str
    text: str = ""
    # text | needs_ocr | closed | no_extractor | invalid | retry
    outcome: str = "text"
    error_code: str = ""
    message: str = ""


def extract_cpu(path: str) -> Extract:
    """打开一份主件，尽量拿出文字。扫描件只标记 needs_ocr，不在这里识别。"""
    ext = extension_of(path)
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return Extract(path, ext, outcome="retry", error_code="RETRYABLE_IO", message=str(exc))
    if size == 0:
        return Extract(path, ext, outcome="invalid", error_code="ZERO_BYTES", message="empty file")
    if ext in CLOSED or ext == "":
        return Extract(path, ext, outcome="closed", error_code="CLOSED_TYPE", message=ext or "no extension")
    if ext in NO_EXTRACTOR:
        return Extract(path, ext, outcome="no_extractor", error_code="NO_EXTRACTOR", message=ext)
    if ext in IMAGE:
        return Extract(path, ext, outcome="needs_ocr", error_code="IMAGE", message=ext)
    if ext in PLAIN:
        return Extract(path, ext, text=_read_plain(path)[:8000], outcome="text")
    if ext in OOXML:
        return _ooxml(path, ext)
    if ext in PDF:
        return _pdf_text_layer(path)
    return Extract(path, ext, outcome="closed", error_code="CLOSED_TYPE", message=ext)


def _read_plain(path: str) -> str:
    raw = open(path, "rb").read()
    text = raw.decode("utf-8", "replace")
    if path.lower().endswith(".rtf"):
        text = re.sub(r"\\[a-z]+\d* ?", " ", text)
        text = re.sub(r"[{}]", " ", text)
    elif path.lower().endswith((".html", ".htm", ".xml")):
        text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _ooxml(path: str, ext: str) -> Extract:
    try:
        text = _ooxml_text(path, ext)
    except (zipfile.BadZipFile, ET.ParseError, KeyError, OSError) as exc:
        return Extract(path, ext, outcome="invalid", error_code="CORRUPT", message=str(exc))
    return Extract(path, ext, text=text[:8000], outcome="text")


def _ooxml_text(path: str, ext: str) -> str:
    chunks: list[str] = []
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        if ext in {".docx", ".docm"}:
            names = [n for n in names if n.startswith("word/") and n.endswith(".xml")]
        elif ext in {".pptx", ".pptm"}:
            names = [n for n in names if n.startswith("ppt/slides/") and n.endswith(".xml")]
        elif ext in {".xlsx", ".xlsm"}:
            names = [n for n in names if n.endswith(".xml") and ("sharedStrings" in n or "/worksheets/" in n)]
        for name in names:
            chunks.extend(_xml_pieces(zf.read(name)))
    return "\n".join(chunks).strip()


def _xml_pieces(blob: bytes) -> list[str]:
    try:
        root = ET.fromstring(blob)
    except ET.ParseError:
        return []
    return [node.text.strip() for node in root.iter() if node.tag in _XML_TEXT and node.text and node.text.strip()]


def _pdf_text_layer(path: str) -> Extract:
    try:
        import pymupdf
    except ImportError:
        return Extract(path, ".pdf", outcome="needs_ocr", error_code="NO_PYMUPDF", message="pymupdf missing")
    try:
        doc = pymupdf.open(path)
    except Exception as exc:
        return _pdf_open_failed(path, exc)
    try:
        if getattr(doc, "needs_pass", False):
            return Extract(path, ".pdf", outcome="invalid", error_code="ENCRYPTED", message="encrypted")
        if doc.page_count == 0:
            return Extract(path, ".pdf", outcome="invalid", error_code="ZERO_PAGES", message="zero pages")
        parts = [(page.get_text("text") or "") for page in doc]
    except Exception as exc:
        return Extract(path, ".pdf", outcome="retry", error_code="RETRYABLE_IO", message=str(exc))
    finally:
        doc.close()
    text = "\n".join(parts).strip()
    if len(text) < _MIN_TEXT:
        return Extract(path, ".pdf", text=text, outcome="needs_ocr", error_code="NO_TEXT_LAYER")
    return Extract(path, ".pdf", text=text[:8000], outcome="text")


def _pdf_open_failed(path: str, exc: Exception) -> Extract:
    message = str(exc).lower()
    if "password" in message or "encrypt" in message:
        return Extract(path, ".pdf", outcome="invalid", error_code="ENCRYPTED", message=str(exc))
    if isinstance(exc, (TimeoutError,)):
        return Extract(path, ".pdf", outcome="retry", error_code="RETRYABLE_IO", message=str(exc))
    return Extract(path, ".pdf", outcome="invalid", error_code="CORRUPT", message=str(exc))
