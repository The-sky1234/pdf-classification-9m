from __future__ import annotations
import re

_PATENT_NO = re.compile(
    r"(CN\s*\d{8,}[A-Z]?|ZL\s*\d{8,}|申请公布号\s*[:：]?\s*[A-Z0-9.]{8,}|授权公告号\s*[:：]?\s*[A-Z0-9.]{8,})",
    re.I)
_PATENT_WORD = ("专利", "发明公开", "实用新型", "外观设计")
LV03_CODES = {"CORRUPT_UNRECOVERABLE", "ZERO_PAGES", "ENCRYPTED_UNREADABLE"}


def check_patent(file_id: str, feature: dict, dedup: dict | None = None) -> dict:
    """证据不足时不能给出「不是专利」。专利文献范围未批准时，只有编号加文献词才确认。"""
    blob = " ".join([
        feature.get("filename") or "",
        feature.get("title") or "",
        feature.get("query_text") or "",
    ])
    has_number = bool(_PATENT_NO.search(blob))
    has_word = any(w in blob for w in _PATENT_WORD)
    text_missing = bool(feature.get("text_missing") or feature.get("ocr_failed"))
    if text_missing and not (has_number and has_word):
        return {"file_id": file_id, "status": "NEED_REVIEW", "patent": None,
                "reason": "text_missing_cannot_exclude_patent",
                "content_version": feature.get("content_version")}
    if has_number and has_word:
        return {"file_id": file_id, "status": "APPROVED", "patent": "hit",
                "archive_type": "PATENT", "reason": "patent_number",
                "content_version": feature.get("content_version")}
    if has_word or has_number:
        return {"file_id": file_id, "status": "NEED_REVIEW", "patent": None,
                "reason": "patent_scope_unapproved",
                "content_version": feature.get("content_version")}
    if not (feature.get("query_text") or feature.get("title")):
        return {"file_id": file_id, "status": "NEED_REVIEW", "patent": None,
                "reason": "no_text_cannot_exclude_patent",
                "content_version": feature.get("content_version")}
    return {"file_id": file_id, "status": "PASS", "patent": "miss",
            "content_version": feature.get("content_version")}


def check_low_value(file_id: str, feature: dict, dedup: dict | None = None,
                    validity: str | None = None) -> dict:
    """只自动确认 LV02（精确重复且主件不是专利）和 LV03（有效性已确认）。

    LV01 阈值和 LV05 词表未批准，不自动排除。OCR 失败不改写成 LV01 或 LV03。
    """
    cv = feature.get("content_version")
    if feature.get("ocr_failed") and validity not in LV03_CODES:
        return {"file_id": file_id, "status": "NEED_REVIEW", "reason": "ocr_failed_not_low_value",
                "reasons": [], "content_version": cv}
    reasons: list[str] = []
    if validity in LV03_CODES:
        reasons.append("LV03")
    dup = dedup or {}
    if dup.get("is_exact_dup") and not dup.get("master_is_patent"):
        reasons.append("LV02")
    elif dup.get("is_exact_dup") and dup.get("master_is_patent"):
        return {"file_id": file_id, "status": "NEED_REVIEW", "reason": "patent_copy_not_duplicate_dir",
                "reasons": [], "content_version": cv}
    if len(reasons) > 1:
        return {"file_id": file_id, "status": "NEED_REVIEW", "reason": "multiple_low_value",
                "reasons": reasons, "content_version": cv}
    if len(reasons) == 1:
        return {"file_id": file_id, "status": "APPROVED", "archive_type": "LOW_VALUE",
                "reason": reasons[0], "reasons": reasons, "content_version": cv}
    return {"file_id": file_id, "status": "PASS", "reasons": [], "content_version": cv}
