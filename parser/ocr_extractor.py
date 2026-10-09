from __future__ import annotations
from typing import Callable, Optional
from parser.models import BBox, Span, PageContent


def render_page_to_png_bytes(doc, page_no: int, dpi: int = 200) -> bytes:
    page = doc[page_no - 1]
    pix = page.get_pixmap(dpi=dpi)
    return pix.tobytes("png")


def ocr_pages(doc, page_nos: list[int], ocr_backend: Optional[Callable] = None,
              dpi: int = 200) -> list[PageContent]:
    if ocr_backend is None:
        from parser.ocr_paddle import paddle_ocr_backend as ocr_backend
    out: list[PageContent] = []
    for pn in page_nos:
        img = render_page_to_png_bytes(doc, pn, dpi=dpi)
        lines = ocr_backend(img)
        spans = [Span(text=t, bbox=BBox(*b), size=0.0, font="ocr", page_no=pn, ocr_score=c)
                 for (t, b, c) in lines]
        text = "".join(s.text for s in spans)
        score = sum(s.ocr_score or 0 for s in spans) / len(spans) if spans else 0.0
        out.append(PageContent(page_no=pn, text=text, spans=spans,
                               image_count=1, char_count=len(text.strip()), ocr_score=score))
    return out
