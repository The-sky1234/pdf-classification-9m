from __future__ import annotations
import os
from parser.models import ParseResult, ParseErrorInfo, ErrorCode, EvidenceMode
from parser.errors import ParseError, classify_open_exception
from parser.detector import SCAN_TEXT_MAX, decide_type
from parser.pymupdf_extractor import extract_pages
from parser.ocr_extractor import ocr_pages
from parser.router import route
from parser.limits import LABEL_PAGES


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


def parse_pdf(file_path: str, ocr_backend=None, mineru=None, max_pages: int | None = None,
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
            limit = LABEL_PAGES if max_pages is None else max_pages
            pages, stats, headings, title = extract_pages(doc, max_pages=limit)
            truncated = bool(limit and len(doc) > limit)
            pdf_type = decide_type(len(stats) or len(doc), stats)
            plan = route(pdf_type, has_title=bool(title.strip()))
            if force_mineru and pdf_type not in ("COMPLEX_PDF", "MIXED_PDF"):
                return ParseResult(file_path=file_path, pdf_type=pdf_type, page_count=len(doc),
                                   ok=False, error=ParseErrorInfo(ErrorCode.MINERU_FAILED,
                                   "mineru forbidden for non-complex pdf", False))
            needed = [page.page_no for page, stat in zip(pages, stats)
                      if stat.get("char_count", 0) <= SCAN_TEXT_MAX]
            has_direct_text = any(stat.get("char_count", 0) > SCAN_TEXT_MAX for stat in stats)
            evidence = plan["evidence_mode"]
            backend = plan["backend"]
            mean = None
            if needed:
                try:
                    recognized = {item.page_no: item for item in ocr_pages(
                        doc, needed, ocr_backend=ocr_backend)}
                except Exception as e:
                    # 没装 OCR 引擎时再试也没有结果，交给复核。超时一类的失败仍可重试。
                    missing_engine = isinstance(e, ModuleNotFoundError) or "No module named" in str(e)
                    return ParseResult(file_path=file_path, pdf_type=pdf_type, page_count=len(doc),
                                       ok=False, error=ParseErrorInfo(
                                           ErrorCode.OCR_FAILED, str(e), not missing_engine))
                pages = [recognized.get(page.page_no, page) for page in pages]
                scores = [page.ocr_score or 0 for page in pages if page.ocr_score is not None]
                mean = sum(scores) / len(scores) if scores else 0.0
                evidence = EvidenceMode.OCR_BODY
                backend = "pymupdf+paddleocr" if has_direct_text else "paddleocr"
            else:
                evidence = plan["evidence_mode"] if title.strip() else EvidenceMode.BODY
                if evidence == EvidenceMode.OCR_BODY:
                    evidence = EvidenceMode.BODY
                backend = "pymupdf"
            if pdf_type == "COMPLEX_PDF" and mineru is not None and not needed:
                try:
                    pages = list(mineru.parse(file_path))
                    if limit:
                        pages = pages[:limit]
                except ParseError as e:
                    return ParseResult(file_path=file_path, pdf_type=pdf_type, page_count=len(doc),
                                       ok=False, error=e.info)
            return ParseResult(file_path=file_path, pdf_type=pdf_type,
                               evidence_mode=evidence, page_count=len(doc),
                               title=title, headings=headings, pages=pages,
                               ocr_mean_score=mean, ok=True, backend=backend,
                               truncated=truncated)
        except ParseError as e:
            return ParseResult(file_path=file_path, ok=False, error=e.info)
        except Exception as e:
            info = classify_open_exception(e)
            return ParseResult(file_path=file_path, ok=False, error=info)
