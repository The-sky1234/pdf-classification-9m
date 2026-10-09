from __future__ import annotations
from classifier.config import Thresholds

# data-contract/state-machine取值：TITLE | BODY | OCR_BODY | VISION
MODES = ("TITLE", "BODY", "OCR_BODY", "VISION")
GENERIC_WORDS = {"通知", "工作", "管理", "会议", "精神", "要求", "部署", "落实",
                 "notice", "meeting", "work", "management"}
TITLE_MIN_LEN = 6


def _paras(pages: list[dict], limit: int) -> list[dict]:
    out: list[dict] = []
    for pg in pages:
        text = (pg.get("text") or "")
        for para in [p.strip() for p in text.split("\n") if p.strip()]:
            if len(para) >= 10:
                out.append({"text": para[:600], "page_no": pg.get("page_no", 1)})
        if len(out) >= limit:
            break
    return out[:limit]


def _is_generic_title(title: str) -> bool:
    core = "".join(ch for ch in title if ch.strip())
    if not core:
        return True
    hits = sum(1 for g in GENERIC_WORDS if g in title)
    substantive = sum(1 for ch in core if ch not in "".join(GENERIC_WORDS))
    return hits > 0 and substantive < 4


def route_evidence(parse: dict, th: Thresholds) -> dict:
    pages = parse.get("pages", []) or []
    title = (parse.get("title") or "").strip()
    headings = parse.get("headings", []) or []
    has_title = len(title) >= TITLE_MIN_LEN
    generic_only = has_title and _is_generic_title(title)
    raw = str(parse.get("evidence_mode") or "").upper().replace("_MODE", "")
    suggested = raw if raw in MODES else ""
    has_text = any((p.get("text") or "").strip() for p in pages)
    if suggested == "VISION" or (parse.get("pdf_type") == "IMAGE_PDF"
                                 and not has_title and not has_text):
        mode = "VISION"
        bundle = {"title": "", "headings": [], "paras": [],
                  "visual": parse.get("visual_description", "")}
    elif suggested == "OCR_BODY" or parse.get("pdf_type") in ("SCAN_PDF", "IMAGE_PDF"):
        mode = "OCR_BODY"
        bundle = {"title": title if has_title else "", "headings": headings[:5],
                  "paras": _paras(pages, th.max_body_paras), "visual": ""}
    elif has_title and not generic_only:
        mode = "TITLE"
        bundle = {"title": title, "headings": headings[:5],
                  "paras": _paras(pages, 2), "visual": ""}
    else:
        mode = "BODY"
        bundle = {"title": "", "headings": headings[:5],
                  "paras": _paras(pages, th.max_body_paras), "visual": ""}
    flat = " ".join([bundle["title"]] + [h.get("text", "") if isinstance(h, dict) else str(h)
                                          for h in bundle["headings"]] +
                     [p["text"] for p in bundle["paras"]])[:th.max_evidence_chars]
    if bundle["visual"]:
        flat = (flat + " " + bundle["visual"])[:th.max_evidence_chars]
    return {"mode": mode, "bundle": bundle, "query_text": flat.strip(),
            "has_title": has_title}
