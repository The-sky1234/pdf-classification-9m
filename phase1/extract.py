"""波次 0 的取字。只在 CPU 上做，不加载 OCR、MinerU 或分类模型。

只为分类取证据，不做全文解析：纯文本只读开头，PDF 只看前 PDF_SAMPLE_PAGES 页。
无效文件（LV03）只在有界恢复之后仍确认结构损坏或为空时才判；
加密、格式和扩展名对不上、没有文字层，都不算无效。
"""
from __future__ import annotations

import html
import os
import re
import struct
import zipfile
import zlib
from collections import Counter
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

from phase1.formats import CLOSED, IMAGE, NO_EXTRACTOR, OOXML, PDF, PLAIN, extension_of

_XML_TEXT = (
    "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t",
    "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t",
    "{http://schemas.openxmlformats.org/drawingml/2006/main}t",
)
MAX_TEXT = 8000
PLAIN_HEAD_BYTES = 256 * 1024
PDF_SAMPLE_PAGES = 10
# 一页去掉重复行以后少于这么多字，或者图片盖住大半页而字很少，就当这一页要 OCR。
PAGE_MIN_CHARS = 50
PAGE_IMAGE_COVER = 0.5
PAGE_IMAGE_MAX_CHARS = 200
# 有文字的页已经凑够这么多字、并且首页有字时，扫描页不再送 OCR。
ENOUGH_TEXT = 200
_OOXML_MEMBER_LIMIT = 32 * 1024 * 1024
_RECOVER_SCAN_LIMIT = 64 * 1024 * 1024
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


@dataclass
class Extract:
    path: str
    extension: str
    text: str = ""
    # text | needs_ocr | closed | no_extractor | invalid | encrypted | format_mismatch | retry
    outcome: str = "text"
    error_code: str = ""
    message: str = ""
    page_count: int = 0
    # PDF 里要 OCR 的页（从 0 开始，只在抽样页里）。图片文件为空列表，表示整份都要。
    ocr_pages: list[int] = field(default_factory=list)


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
    try:
        if ext in IMAGE:
            return Extract(path, ext, outcome="needs_ocr", error_code="IMAGE", message=ext)
        if ext in PLAIN:
            return Extract(path, ext, text=_read_plain(path)[:MAX_TEXT], outcome="text")
        if ext in OOXML:
            return _ooxml(path, ext)
        if ext in PDF:
            return _pdf_text_layer(path)
    except (OSError, MemoryError) as exc:
        return Extract(path, ext, outcome="retry", error_code="RETRYABLE_IO", message=str(exc))
    return Extract(path, ext, outcome="closed", error_code="CLOSED_TYPE", message=ext)


def decode_bytes(raw: bytes) -> str:
    """BOM → UTF-8 → GB18030 → UTF-16（无 BOM）→ UTF-8 带替换符。末尾被截断的半个字不算解码失败。"""
    if raw.startswith(b"\xef\xbb\xbf"):
        return _decode_prefix(raw[3:], "utf-8")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return _decode_prefix(raw, "utf-16")
    for codec in ("utf-8", "gb18030"):
        try:
            return _decode_prefix(raw, codec, strict=True)
        except UnicodeDecodeError:
            continue
    head = raw[:4096]
    if head and head.count(b"\x00") > len(head) // 4:
        codec = "utf-16-le" if head[1:2] == b"\x00" else "utf-16-be"
        return _decode_prefix(raw, codec)
    return raw.decode("utf-8", "replace")


def _decode_prefix(raw: bytes, codec: str, strict: bool = False) -> str:
    try:
        return raw.decode(codec)
    except UnicodeDecodeError as exc:
        if exc.start >= len(raw) - 4:
            return raw[: exc.start].decode(codec)
        if strict:
            raise
        return raw.decode(codec, "replace")


def _read_plain(path: str) -> str:
    with open(path, "rb") as handle:
        raw = handle.read(PLAIN_HEAD_BYTES)
    lower = path.lower()
    if lower.endswith(".rtf"):
        text = rtf_to_text(raw)
    else:
        text = decode_bytes(raw)
        if lower.endswith((".html", ".htm", ".xml")):
            text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
            text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"\s+", " ", text).strip()


_RTF_CODEPAGES = {"936": "gbk", "54936": "gb18030", "950": "big5", "65001": "utf-8", "1252": "cp1252"}
_RTF_TOKEN = re.compile(rb"\\'([0-9a-fA-F]{2})|\\u(-?\d+)\??|\\([a-zA-Z]+)(-?\d+)? ?|\\([^a-zA-Z])|([{}])|([^\\{}]+)")
_RTF_SKIP = {b"fonttbl", b"colortbl", b"stylesheet", b"info", b"pict", b"object", b"themedata",
             b"datastore", b"latentstyles", b"listtable", b"listoverridetable", b"rsidtbl", b"generator"}


def rtf_to_text(raw: bytes) -> str:
    """RTF 正文。中文多是 \\'hh 的 GBK 字节或 \\uN，按 \\ansicpg 解码。"""
    match = re.search(rb"\\ansicpg(\d+)", raw[:2048])
    codec = _RTF_CODEPAGES.get(match.group(1).decode() if match else "936", "gbk")
    out: list[str] = []
    pending = bytearray()
    depth = 0
    skip_depth: int | None = None
    star = False

    def flush():
        if pending:
            out.append(bytes(pending).decode(codec, "replace"))
            pending.clear()

    for hexbyte, uni, word, _num, sym, brace, plain in _RTF_TOKEN.findall(raw):
        if brace:
            flush()
            if brace == b"{":
                depth += 1
            else:
                if skip_depth is not None and depth <= skip_depth:
                    skip_depth = None
                depth -= 1
            star = False
            continue
        if skip_depth is not None:
            continue
        if sym == b"*":
            star = True
            continue
        if word:
            if word in _RTF_SKIP or star:
                skip_depth = depth
                star = False
                continue
            if word in (b"par", b"line", b"tab", b"cell", b"row"):
                flush()
                out.append(" ")
            continue
        if hexbyte:
            pending.append(int(hexbyte, 16))
            continue
        if uni:
            flush()
            code = int(uni)
            out.append(chr(code + 65536 if code < 0 else code))
            continue
        if plain:
            flush()
            out.append(plain.decode(codec, "replace").replace("\r", "").replace("\n", ""))
    flush()
    return "".join(out)


def _sniff(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read(1024)


def _ooxml(path: str, ext: str) -> Extract:
    head = _sniff(path)
    if head.startswith(_OLE_MAGIC):
        # 加密的 OOXML 会被 Office 包进 OLE 容器；也可能是改了扩展名的旧版 .doc/.xls。
        return Extract(path, ext, outcome="encrypted", error_code="ENCRYPTED_OR_OLE", message="OLE container")
    if not head.startswith(b"PK"):
        return Extract(path, ext, outcome="format_mismatch", error_code="FORMAT_MISMATCH",
                       message=repr(head[:8]))
    try:
        text = _ooxml_text(path, ext)
        code = ""
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, NotImplementedError, RuntimeError) as exc:
        text = _recover_ooxml(path, ext)
        if not text:
            return Extract(path, ext, outcome="invalid", error_code="CORRUPT", message=f"zip: {exc}")
        code = "RECOVERED"
    if not text.strip():
        return Extract(path, ext, outcome="needs_review", error_code="NO_TEXT", message="ooxml without text")
    return Extract(path, ext, text=text[:MAX_TEXT], outcome="text", error_code=code)


def _member_filter(ext: str):
    if ext in {".docx", ".docm"}:
        return lambda n: n.startswith("word/") and n.endswith(".xml") and (
            "document" in n or n.startswith(("word/header", "word/footnotes")))
    if ext in {".pptx", ".pptm"}:
        return lambda n: n.startswith("ppt/slides/slide") and n.endswith(".xml")
    return lambda n: n.endswith(".xml") and ("sharedStrings" in n or "/worksheets/" in n)


def _ooxml_text(path: str, ext: str) -> str:
    chunks: list[str] = []
    total = 0
    keep = _member_filter(ext)
    with zipfile.ZipFile(path) as zf:
        names = sorted((n for n in zf.namelist() if keep(n)), key=_member_order)
        for name in names:
            with zf.open(name) as member:
                blob = member.read(_OOXML_MEMBER_LIMIT)
            for piece in _xml_pieces(blob):
                chunks.append(piece)
                total += len(piece)
            if total >= MAX_TEXT:
                break
    return "\n".join(chunks).strip()


def _member_order(name: str):
    if name.endswith("document.xml") or "sharedStrings" in name:
        return (0, name)
    digits = re.findall(r"\d+", name)
    return (1, int(digits[-1]) if digits else 0, name)


def _recover_ooxml(path: str, ext: str) -> str:
    """中央目录坏了（常见于拷贝中断）时，顺着本地文件头逐个解压，能拿多少拿多少。"""
    keep = _member_filter(ext)
    with open(path, "rb") as handle:
        data = handle.read(_RECOVER_SCAN_LIMIT)
    chunks: list[str] = []
    pos = 0
    while True:
        pos = data.find(b"PK\x03\x04", pos)
        if pos < 0 or pos + 30 > len(data):
            break
        method = struct.unpack("<H", data[pos + 8:pos + 10])[0]
        name_len, extra_len = struct.unpack("<HH", data[pos + 26:pos + 30])
        name = data[pos + 30:pos + 30 + name_len].decode("utf-8", "replace")
        start = pos + 30 + name_len + extra_len
        pos = start
        if not keep(name):
            continue
        if method == 8:
            blob = zlib.decompressobj(-15).decompress(data[start:], _OOXML_MEMBER_LIMIT)
        elif method == 0:
            blob = data[start:start + _OOXML_MEMBER_LIMIT]
        else:
            continue
        chunks.extend(_xml_pieces(blob, partial=True))
    return "\n".join(chunks).strip()


def _xml_pieces(blob: bytes, partial: bool = False) -> list[str]:
    try:
        root = ET.fromstring(blob)
    except ET.ParseError:
        if not partial:
            return []
        found = re.findall(rb"<(?:w|a|x)?:?t(?:\s[^>]*)?>([^<]+)</", blob)
        return [html.unescape(v.decode("utf-8", "replace")).strip() for v in found if v.strip()]
    return [node.text.strip() for node in root.iter() if node.tag in _XML_TEXT and node.text and node.text.strip()]


def _pdf_text_layer(path: str) -> Extract:
    try:
        import pymupdf
    except ImportError:
        return Extract(path, ".pdf", outcome="retry", error_code="NO_PYMUPDF", message="pymupdf missing")
    head = _sniff(path)
    if b"%PDF" not in head:
        return Extract(path, ".pdf", outcome="format_mismatch", error_code="FORMAT_MISMATCH",
                       message=repr(head[:8]))
    try:
        doc = pymupdf.open(path)
    except Exception as exc:
        return _pdf_open_failed(path, exc)
    try:
        if doc.needs_pass and not doc.authenticate(""):
            return Extract(path, ".pdf", outcome="encrypted", error_code="ENCRYPTED", message="user password")
        if doc.page_count == 0:
            return Extract(path, ".pdf", outcome="invalid", error_code="ZERO_PAGES", message="zero pages")
        sample = min(doc.page_count, PDF_SAMPLE_PAGES)
        pages = [_page_sample(doc[i]) for i in range(sample)]
        page_count = doc.page_count
    except Exception as exc:
        return Extract(path, ".pdf", outcome="retry", error_code="RETRYABLE_IO", message=str(exc))
    finally:
        doc.close()
    texts = strip_repeated_lines([text for text, _ in pages])
    needy = [i for i, (text, (_, cover)) in enumerate(zip(texts, pages))
             if len(_compact(text)) < PAGE_MIN_CHARS
             or (cover >= PAGE_IMAGE_COVER and len(_compact(text)) < PAGE_IMAGE_MAX_CHARS)]
    good = "\n".join(t for i, t in enumerate(texts) if i not in needy).strip()
    if needy and (0 in needy or len(_compact(good)) < ENOUGH_TEXT):
        return Extract(path, ".pdf", text=good[:MAX_TEXT], outcome="needs_ocr", error_code="NO_TEXT_LAYER",
                       page_count=page_count, ocr_pages=needy)
    return Extract(path, ".pdf", text=good[:MAX_TEXT], outcome="text", page_count=page_count)


def _page_sample(page) -> tuple[str, float]:
    text = page.get_text("text") or ""
    area = abs(page.rect) or 1.0
    covered = 0.0
    for info in page.get_image_info():
        bbox = page.rect & info.get("bbox", (0, 0, 0, 0))
        covered += abs(bbox)
    return text, min(1.0, covered / area)


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def strip_repeated_lines(page_texts: list[str]) -> list[str]:
    """去掉在多数抽样页上都出现的行（水印、页眉页脚），这些字不代表这一页有正文。"""
    if len(page_texts) < 2:
        return page_texts
    seen = Counter()
    for text in page_texts:
        seen.update({line.strip() for line in text.splitlines() if line.strip()})
    limit = max(2, int(len(page_texts) * 0.6 + 0.5))
    repeated = {line for line, n in seen.items() if n >= limit}
    if not repeated:
        return page_texts
    return ["\n".join(line for line in text.splitlines() if line.strip() not in repeated)
            for text in page_texts]


def _pdf_open_failed(path: str, exc: Exception) -> Extract:
    message = str(exc).lower()
    if "password" in message or "encrypt" in message:
        return Extract(path, ".pdf", outcome="encrypted", error_code="ENCRYPTED", message=str(exc))
    if isinstance(exc, (OSError, MemoryError)):
        return Extract(path, ".pdf", outcome="retry", error_code="RETRYABLE_IO", message=str(exc))
    # MuPDF 打开时已经做过修复，修不了才走到这里。
    return Extract(path, ".pdf", outcome="invalid", error_code="CORRUPT", message=str(exc))
