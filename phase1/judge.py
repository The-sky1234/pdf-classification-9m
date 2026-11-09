"""一份文件的结论。

先看取字结果和规则。规则定了就结束。
还要 OCR 或还要分类模型时，settled 为假，调用方不要复制进正式目录。
优先级：专利 > 低价值 > 业务 > 待复核。
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field

from classifier.taxonomy_v3 import target_dir
from phase1.extract import Extract, extract_cpu
from phase1.rules import is_invalid, patent_status

# 这些原因说明文件本身拿不到文字；落到待复核时原样保留，不改写成「无法排除专利」。
_NO_TEXT_REASONS = {"retry", "closed_type", "no_extractor", "needs_ocr", "encrypted", "format_mismatch",
                    "no_text", "ocr_no_text"}


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
    page_count: int = 0
    ocr_pages: list[int] = field(default_factory=list)
    error_code: str = ""
    scores: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def judge_file(path: str, classify_fn=None, extracted: Extract | None = None) -> Decision:
    """classify_fn 只在波次 2 传入。波次 0 不传，有文字但规则定不了的文件会标明 next_wave=classify。

    PENDING 结论里的 level1/level2 只是模型建议，不是归档目录。
    """
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
    if got.outcome == "encrypted":
        return _pending(got, "encrypted", text_missing=True)
    if got.outcome == "format_mismatch":
        return _pending(got, "format_mismatch", text_missing=True)
    if got.outcome == "needs_review":
        return _pending(got, "no_text", text_missing=True)
    if got.outcome == "needs_ocr":
        if patent_status(filename, got.text, text_missing=False) == "approved":
            return _final(got, "PATENT", "APPROVED", "PATENT")
        return _defer(got, "ocr", "needs_ocr")
    return _from_text(got, filename, classify_fn)


def _from_text(got: Extract, filename: str, classify_fn) -> Decision:
    if not got.text.strip():
        return _pending(got, "ocr_no_text" if got.error_code == "OCR" else "no_text", text_missing=True)
    status = patent_status(filename, got.text, text_missing=False)
    if status == "approved":
        return _final(got, "PATENT", "APPROVED", "PATENT")
    if status == "review":
        return _pending(got, "patent_uncertain", text_missing=False)
    if classify_fn is None:
        return _defer(got, "classify", "awaiting_classify")
    return _apply_classifier(got, classify_fn(classify_query(got.path, got.text)))


def classify_query(path: str, text: str) -> str:
    """分类证据：文件名（多数就是标题）在前，正文开头在后。"""
    stem = os.path.splitext(os.path.basename(path.replace("\\", "/")))[0]
    return f"{stem}\n{text}"


def _apply_classifier(got: Extract, result: dict) -> Decision:
    calibrated = bool(result.get("calibrated"))
    level1 = result.get("level1")
    level2 = result.get("level2")
    scores = result.get("scores") or {}
    if result.get("confirmed_unmatched") and calibrated:
        row = _final(got, "LOW_VALUE", "APPROVED", "LV04")
        row.scores = scores
        return row
    if calibrated and level1 and level2:
        try:
            rel = target_dir("BUSINESS", None, level1, level2)
        except (ValueError, KeyError):
            return _pending(got, "illegal_parent", text_missing=False)
        row = _final(got, "BUSINESS", "APPROVED", "BUSINESS", level1, level2)
        row.target_dir = rel
        row.scores = scores
        return row
    reason = result.get("reason") or "uncalibrated_or_uncertain"
    if not calibrated and level1 and level2:
        reason = "uncalibrated_thresholds_blocked"
    row = _pending(got, reason, text_missing=False, level1=level1, level2=level2)
    row.scores = scores
    return row


def _common(got: Extract) -> dict:
    return {"source_path": got.path, "extension": got.extension, "text_chars": len(got.text),
            "page_count": got.page_count, "error_code": got.error_code}


def _final(got, archive_type, decision_status, reason, level1=None, level2=None) -> Decision:
    rel = target_dir(archive_type, None, level1, level2, reason if archive_type == "LOW_VALUE" else None)
    return Decision(settled=True, next_wave="", archive_type=archive_type, decision_status=decision_status,
                    reason=reason, level1=level1, level2=level2, target_dir=rel, **_common(got))


def _pending(got, reason, *, text_missing: bool, level1=None, level2=None) -> Decision:
    if text_missing and reason not in _NO_TEXT_REASONS:
        reason = "text_missing_cannot_exclude_patent"
    return Decision(settled=True, next_wave="", archive_type="PENDING", decision_status="NEED_REVIEW",
                    reason=reason, level1=level1, level2=level2, target_dir=target_dir("PENDING", None),
                    **_common(got))


def _defer(got, wave: str, reason: str) -> Decision:
    return Decision(settled=False, next_wave=wave, archive_type="", decision_status="", reason=reason,
                    target_dir="", ocr_pages=list(got.ocr_pages), **_common(got))
