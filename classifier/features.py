from __future__ import annotations
import hashlib
from classifier.config import Thresholds
from classifier.evidence import route_evidence

MAX_QUERY = 3000


def build(parse: dict) -> dict:
    """收成各门控共用的短证据。解析失败不编造正文。query_text 不超过 3000 字。"""
    if not parse or parse.get("ok") is False:
        return {"query_text": "", "page_evidence": [], "text_fingerprint": "",
                "title": "", "text_missing": True, "mode": None}
    ev = route_evidence(parse, Thresholds(max_evidence_chars=MAX_QUERY))
    query = (ev.get("query_text") or "")[:MAX_QUERY]
    pages = []
    for p in ev.get("bundle", {}).get("paras") or []:
        pages.append({"page_no": p.get("page_no"), "text": p.get("text")})
    if ev.get("bundle", {}).get("title"):
        pages.insert(0, {"page_no": 1, "text": ev["bundle"]["title"]})
    return {"query_text": query,
            "page_evidence": pages,
            "text_fingerprint": hashlib.sha256(query.encode("utf-8")).hexdigest() if query else "",
            "title": ev.get("bundle", {}).get("title") or "",
            "text_missing": not bool(query),
            "ocr_failed": bool(parse.get("ocr_failed")),
            "mode": ev.get("mode")}
