from __future__ import annotations
import os
from classifier.features import build
from classifier.taxonomy_v3 import target_dir
from exclusion.gates import LV03_CODES, check_low_value, check_patent


def _decision(file_id: str, archive_type: str, decision_status: str, content_version,
              level1=None, level2=None, reason=None, action="DECIDE") -> dict:
    rel = target_dir(archive_type, None, level1, level2, reason if archive_type == "LOW_VALUE" else None)
    return {"action": action, "file_id": file_id, "archive_type": archive_type,
            "decision_status": decision_status, "level1": level1, "level2": level2,
            "reason": reason, "target_dir": rel, "content_version": content_version,
            "result_type": _result_type(archive_type, decision_status)}


def _result_type(archive_type: str, decision_status: str) -> str:
    if decision_status == "NEED_REVIEW":
        return "MANUAL_REVIEW"
    if archive_type == "BUSINESS":
        return "CLASSIFIED"
    return "IRRELEVANT"


def decide(file_id: str, parse: dict, dedup: dict | None = None, classify_fn=None,
           content_version: int | None = None) -> dict:
    """专利 → 低价值四类 → 相关性/27 场景。不拼自由路径，不在这里复制文件。"""
    err = (parse or {}).get("error") or {}
    code = err.get("code") if isinstance(err, dict) else getattr(err, "code", None)
    retryable = err.get("retryable") if isinstance(err, dict) else getattr(err, "retryable", False)
    if parse is not None and parse.get("ok") is False:
        if retryable:
            return {"action": "RETRY", "error_code": code, "file_id": file_id}
        if code in LV03_CODES:
            return _decision(file_id, "LOW_VALUE", "APPROVED", content_version, reason="LV03")
        return _decision(file_id, "PENDING", "NEED_REVIEW", content_version, reason=code or "parse_failed")
    feature = build(parse or {})
    feature["filename"] = os.path.basename((parse or {}).get("file_path") or "")
    feature["content_version"] = content_version
    if (parse or {}).get("pdf_type") in ("SCAN_PDF", "IMAGE_PDF") and not feature.get("query_text"):
        feature["text_missing"] = True
    patent = check_patent(file_id, feature, dedup)
    if patent["status"] == "APPROVED":
        return _decision(file_id, "PATENT", "APPROVED", content_version, reason="PATENT")
    if patent["status"] == "NEED_REVIEW":
        return _decision(file_id, "PENDING", "NEED_REVIEW", content_version, reason=patent["reason"])
    validity = code if code in LV03_CODES else None
    low = check_low_value(file_id, feature, dedup, validity=validity)
    if low["status"] == "APPROVED":
        return _decision(file_id, "LOW_VALUE", "APPROVED", content_version, reason=low["reason"])
    if low["status"] == "NEED_REVIEW":
        return _decision(file_id, "PENDING", "NEED_REVIEW", content_version, reason=low["reason"])
    if classify_fn is None:
        return _decision(file_id, "PENDING", "NEED_REVIEW", content_version, reason="no_classifier")
    classified = classify_fn(parse)
    rt = classified.get("result_type")
    l1, l2 = classified.get("level1"), classified.get("level2")
    if rt == "IRRELEVANT" and classified.get("calibrated") and classified.get("confirmed_unmatched"):
        return _attach_model(_decision(file_id, "LOW_VALUE", "APPROVED", content_version, reason="LV04"),
                             classified)
    if rt == "CLASSIFIED" and l1 and l2 and classified.get("calibrated"):
        try:
            target_dir("BUSINESS", None, l1, l2)
        except AssertionError:
            return _attach_model(_decision(file_id, "PENDING", "NEED_REVIEW", content_version,
                                           reason="illegal_parent"), classified)
        return _attach_model(_decision(file_id, "BUSINESS", "APPROVED", content_version,
                                       level1=l1, level2=l2), classified)
    return _attach_model(_decision(file_id, "PENDING", "NEED_REVIEW", content_version,
                                   level1=l1, level2=l2,
                                   reason=classified.get("reason") or rt or "uncertain"), classified)


def _attach_model(decision: dict, classified: dict) -> dict:
    for key in ("model_version", "decision_source", "prompt_version", "candidate_scores"):
        value = classified.get(key)
        if value not in (None, "", {}):
            decision[key] = value
    return decision
