from __future__ import annotations
from parser.models import BBox, Span, PageContent
from parser.detector import COMPLEX_MIN_SPAN_SIZES


def extract_pages(doc, max_pages: int = 0) -> tuple[list[PageContent], list[dict], list[dict], str]:
    pages: list[PageContent] = []
    stats: list[dict] = []
    headings: list[dict] = []
    title = ""
    n = len(doc)
    limit = n if not max_pages else min(n, max_pages)
    for i in range(limit):
        page = doc[i]
        d = page.get_text("dict")
        spans: list[Span] = []
        chars: list[str] = []
        sizes: set[float] = set()
        tables = 0
        try:
            tables = len(page.find_tables().tables)
        except Exception:
            tables = 0
        for block in d.get("blocks", []):
            if block.get("type", 0) != 0:
                continue
            for line in block.get("lines", []):
                for s in line.get("spans", []):
                    t = s.get("text", "")
                    if t:
                        chars.append(t)
                    sizes.add(round(float(s.get("size", 0)), 1))
                    spans.append(Span(text=t, bbox=BBox(*s.get("bbox", (0, 0, 0, 0))),
                                      size=float(s.get("size", 0)), font=str(s.get("font", "")),
                                      page_no=i + 1))
        text = "".join(chars)
        images = len(page.get_images(full=True))
        is_complex = bool(tables >= 1 or len(sizes) >= COMPLEX_MIN_SPAN_SIZES)
        stats.append({"char_count": len(text.strip()), "image_count": images, "is_complex": is_complex})
        pages.append(PageContent(page_no=i + 1, text=text, spans=spans, image_count=images,
                                 char_count=len(text.strip())))
    ordered = sorted([s for p in pages for s in p.spans], key=lambda s: -s.size)
    if ordered:
        top = ordered[0].size
        seen: set[str] = set()
        for s in ordered:
            t = s.text.strip()
            if not t or t in seen or len(t) > 120:
                continue
            if s.size >= top - 0.5:
                if not title:
                    title = t
                headings.append({"text": t, "page_no": s.page_no, "size": s.size,
                                 "bbox": s.bbox.to_dict()})
                seen.add(t)
            if len(headings) >= 20:
                break
    return pages, stats, headings, title
