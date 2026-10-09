from __future__ import annotations
import os
from parser.models import ParseResult, ParseErrorInfo, ErrorCode, EvidenceMode
from parser.errors import ParseError, classify_open_exception
from parser.detector import decide_type
from parser.pymupdf_extractor import extract_pages
from parser.ocr_extractor import ocr_pages
from parser.router import route


def _open_readonly(file_path: str):
    import pymupdf
    if not os.path.exists(file_path):
        raise ParseError(ErrorCode.CORRUPT, f"file not found: {file_path}", retryable=False)
    try:
        return pymupdf.open(file_path)
    except Exception as e:
        raise classify_open_exception(e)


def detect_only(file_path: str) -> ParseResult:
    doc = _open_readonly(file_path)
    with doc:
        if len(doc) == 0:
            return ParseResult(file_path=file_path, ok=False,
                               error=ParseErrorInfo(ErrorCode.ZERO_PAGES, "zero pages", False))
        if doc.needs_pass:
            return ParseResult(file_path=file_path, ok=False,
                               error=ParseErrorInfo(ErrorCode.ENCRYPTED, "encrypted", False))
        _, stats, _, _ = extract_pages(doc)
        return ParseResult(file_path=file_path, pdf_type=decide_type(len(doc), stats),
                           page_count=len(doc), ok=True)


def parse_pdf(file_path: str, ocr_backend=None, mineru=None, max_pages: int = 0,
              force_mineru: bool = False) -> ParseResult:
    try:
        doc = _open_readonly(file_path)
    except ParseError as e:
        return ParseResult(file_path=file_path, ok=False, error=e.info)
    with doc:
        try:
            if doc.needs_pass:
                return ParseResult(file_path=file_path, ok=False,
                                   error=ParseErrorInfo(ErrorCode.ENCRYPTED, "encrypted/unreadable", False))
            if len(doc) == 0:
                return ParseResult(file_path=file_path, ok=False,
                                   error=ParseErrorInfo(ErrorCode.ZERO_PAGES, "zero pages", False))
            pages, stats, headings, title = extract_pages(doc, max_pages=max_pages)
            truncated = bool(max_pages and len(doc) > max_pages)
            pdf_type = decide_type(len(doc), stats)
            plan = route(pdf_type, has_title=bool(title.strip()))
            if force_mineru and pdf_type not in ("COMPLEX_PDF", "MIXED_PDF"):
                return ParseResult(file_path=file_path, pdf_type=pdf_type, page_count=len(doc),
                                   ok=False, error=ParseErrorInfo(ErrorCode.MINERU_FAILED,
                                   "mineru forbidden for non-complex pdf", False))
            if pdf_type in ("SCAN_PDF", "IMAGE_PDF"):
                try:
                    needed = list(range(1, len(pages) + 1))
                    oc = ocr_pages(doc, needed, ocr_backend=ocr_backend)
                    pages = oc
                except Exception as e:
                    return ParseResult(file_path=file_path, pdf_type=pdf_type, page_count=len(doc),
                                       ok=False, error=ParseErrorInfo(ErrorCode.OCR_FAILED, str(e), True))
                scores = [p.ocr_score or 0 for p in pages]
                mean = sum(scores) / len(scores) if scores else 0.0
                return ParseResult(file_path=file_path, pdf_type=pdf_type,
                                   evidence_mode=EvidenceMode.OCR_BODY, page_count=len(doc),
                                   title="", headings=[], pages=pages, ocr_mean_score=mean,
                                   ok=True, backend=plan["backend"], truncated=truncated)
            if pdf_type == "COMPLEX_PDF" and mineru is not None:
                try:
                    pages = mineru.parse(file_path)
                except ParseError as e:
                    return ParseResult(file_path=file_path, pdf_type=pdf_type, page_count=len(doc),
                                       ok=False, error=e.info)
            if not title.strip():
                plan["evidence_mode"] = EvidenceMode.BODY
            return ParseResult(file_path=file_path, pdf_type=pdf_type,
                               evidence_mode=plan["evidence_mode"], page_count=len(doc),
                               title=title, headings=headings, pages=pages,
                               ok=True, backend=plan["backend"], truncated=truncated)
        except ParseError as e:
            return ParseResult(file_path=file_path, ok=False, error=e.info)
        except Exception as e:
            info = classify_open_exception(e)
            return ParseResult(file_path=file_path, ok=False, error=info)
