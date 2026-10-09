from __future__ import annotations
from parser.models import PdfType

TEXT_CHARS_PER_PAGE = 50
SCAN_TEXT_MAX = 20
COMPLEX_MIN_TABLES = 1
COMPLEX_MIN_SPAN_SIZES = 6


def decide_type(n_pages: int, per_page: list[dict]) -> str:
    if n_pages <= 0:
        return PdfType.TEXT_PDF
    text_pages = sum(1 for p in per_page if p.get("char_count", 0) >= TEXT_CHARS_PER_PAGE)
    image_only_pages = sum(1 for p in per_page if p.get("char_count", 0) <= SCAN_TEXT_MAX and p.get("image_count", 0) > 0)
    complex_pages = sum(1 for p in per_page if p.get("is_complex"))
    if complex_pages > 0 and text_pages > 0:
        return PdfType.COMPLEX_PDF
    if text_pages == n_pages:
        return PdfType.TEXT_PDF
    if image_only_pages == n_pages:
        has_any_image = any(p.get("image_count", 0) > 0 for p in per_page)
        return PdfType.IMAGE_PDF if has_any_image else PdfType.SCAN_PDF
    if image_only_pages > 0 and text_pages > 0:
        return PdfType.MIXED_PDF
    if complex_pages > 0:
        return PdfType.COMPLEX_PDF
    if text_pages == 0:
        return PdfType.SCAN_PDF
    return PdfType.MIXED_PDF
