"""波次 0 的规则。只自动确认两件事：专利文献、文件确实无效。

内容不足阈值和否定词表还没批准，这里不自动排除。
精确重复已经在上游去掉，这里也不再判重复件。
"""
from __future__ import annotations

import re

_PATENT_NO = re.compile(
    r"(CN\s*\d{8,}[A-Z]?|ZL\s*\d{8,}|申请公布号\s*[:：]?\s*[A-Z0-9.]{8,}|授权公告号\s*[:：]?\s*[A-Z0-9.]{8,})",
    re.I,
)
_PATENT_WORD = ("专利", "发明公开", "实用新型", "外观设计")
_INVALID = {"ZERO_BYTES", "ZERO_PAGES", "CORRUPT", "ENCRYPTED"}


def patent_status(filename: str, text: str, *, text_missing: bool) -> str:
    """返回 approved、review 或 pass。pass 表示可以继续往下看，不是「已经证明不是专利」。"""
    blob = f"{filename}\n{text}"
    has_number = bool(_PATENT_NO.search(blob))
    has_word = any(word in blob for word in _PATENT_WORD)
    if text_missing and not (has_number and has_word):
        return "review"
    if has_number and has_word:
        return "approved"
    if has_number or has_word:
        return "review"
    if text_missing or not text.strip():
        return "review"
    return "pass"


def is_invalid(error_code: str) -> bool:
    return error_code in _INVALID
