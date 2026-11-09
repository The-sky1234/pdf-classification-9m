"""一份文件的结论。

先看取字结果和规则。规则定了就结束。
还要 OCR 或还要分类模型时，settled 为假，调用方不要复制进正式目录。
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass

from classifier.taxonomy_v3 import target_dir
from phase1.extract import Extract, extract_cpu
from phase1.rules import is_invalid, patent_status


@dataclass
class Decision:
    source_path: str
    extension: str
    settled: bool
    next_wave: str
    archive_type: str
    decision_status: str
    reason: str
    level1: str | None = None
    level2: str | None = None
    target_dir: str = ""
    text_chars: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def judge_file(path: str, classify_fn=None, extracted: Extract | None = None) -> Decision:
    """classify_fn 只在波次 2 传入。波次 0 不传，有文字但规则定不了的文件会标明 next_wave=classify。"""
    got = extracted if extracted is not None else extract_cpu(path)
    filename = os.path.basename(path)
    if got.outcome == "retry":
        return _defer(got, "retry", "retry")
    if got.outcome == "invalid" or is_invalid(got.error_code):
        return _final(got, "LOW_VALUE", "APPROVED", "LV03")
    if got.outcome == "closed":
        return _pending(got, "closed_type", text_missing=True)
    if got.outcome == "no_extractor":
        return _pending(got, "no_extractor", text_missing=True)
    if got.outcome == "needs_ocr":
        return _defer(got, "ocr", "needs_ocr")
    return _from_text(got, filename, classify_fn)


def _from_text(got: Extract, filename: str, classify_fn) -> Decision:
    status = patent_status(filename, got.text, text_missing=False)
    if status == "approved":
        return _final(got, "PATENT", "APPROVED", "PATENT")
    if status == "review":
        return _pending(got, "patent_uncertain", text_missing=False)
    if classify_fn is None:
        return _defer(got, "classify", "awaiting_classify")
    return _apply_classifier(got, classify_fn(got.text))


def _apply_classifier(got: Extract, result: dict) -> Decision:
    calibrated = bool(result.get("calibrated"))
    level1 = result.get("level1")
    level2 = result.get("level2")
    if result.get("confirmed_unmatched") and calibrated:
        return _final(got, "LOW_VALUE", "APPROVED", "LV04")
    if calibrated and level1 and level2:
        try:
            rel = target_dir("BUSINESS", None, level1, level2)
        except (AssertionError, KeyError):
            return _pending(got, "illegal_parent", text_missing=False)
        row = _final(got, "BUSINESS", "APPROVED", "BUSINESS", level1, level2)
        row.target_dir = rel
        return row
    reason = result.get("reason") or "uncalibrated_or_uncertain"
    return _pending(got, reason, text_missing=False, level1=level1, level2=level2)


def _final(got, archive_type, decision_status, reason, level1=None, level2=None) -> Decision:
    rel = target_dir(archive_type, None, level1, level2, reason if archive_type == "LOW_VALUE" else None)
    return Decision(
        source_path=got.path, extension=got.extension, settled=True, next_wave="",
        archive_type=archive_type, decision_status=decision_status, reason=reason,
        level1=level1, level2=level2, target_dir=rel, text_chars=len(got.text),
    )


def _pending(got, reason, *, text_missing: bool, level1=None, level2=None) -> Decision:
    if text_missing and reason not in {"retry", "closed_type", "no_extractor", "needs_ocr"}:
        reason = "text_missing_cannot_exclude_patent"
    rel = target_dir("PENDING", None)
    return Decision(
        source_path=got.path, extension=got.extension, settled=True, next_wave="",
        archive_type="PENDING", decision_status="NEED_REVIEW", reason=reason,
        level1=level1, level2=level2, target_dir=rel, text_chars=len(got.text),
    )


def _defer(got, wave: str, reason: str) -> Decision:
    return Decision(
        source_path=got.path, extension=got.extension, settled=False, next_wave=wave,
        archive_type="", decision_status="", reason=reason,
        target_dir="", text_chars=len(got.text),
    )
