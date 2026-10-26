from __future__ import annotations
import hashlib
from parser.limits import LABEL_PAGES

MAX_QUERY = 8000


def build(parse: dict) -> dict:
    """把前五页正文收成分类证据。解析失败不编造正文。"""
    if not parse or parse.get("ok") is False:
        return {"query_text": "", "page_evidence": [], "text_fingerprint": "",
                "title": "", "text_missing": True, "mode": None}
    selected = _first_pages(parse.get("pages") or [])
    page_evidence = []
    chunks = []
    for page in selected:
        text = str(page.get("text") or "").strip()
        if not text:
            continue
        chunks.append(text)
        page_evidence.append({"page_no": page.get("page_no"), "text": text[:2000]})
    query = "\n".join(chunks)
    title = str(parse.get("title") or "").strip()
    if title and title not in query:
        query = f"{title}\n{query}"
    query = query[:MAX_QUERY]
    return {"query_text": query,
            "page_evidence": page_evidence,
            "text_fingerprint": hashlib.sha256(query.encode("utf-8")).hexdigest() if query else "",
            "title": title,
            "text_missing": not bool(query),
            "ocr_failed": bool(parse.get("ocr_failed")),
            "mode": parse.get("evidence_mode")}


def _first_pages(pages: list) -> list:
    usable = [page for page in pages if isinstance(page, dict)]
    usable.sort(key=lambda page: page.get("page_no") or 0)
    return usable[:LABEL_PAGES]
