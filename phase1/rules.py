"""波次 0 的规则。只自动确认两件事：专利文献、文件确实无效。

内容不足阈值和否定词表还没批准，这里不自动排除。
精确重复已经在上游去掉，这里也不再判重复件。

「提到专利 ≠ 专利文献」：自动认定只看文件开头的著录结构
（INID 代码、国家知识产权局署名、申请号/公告号字段），正文里出现专利号不算。
"""
from __future__ import annotations

import re

HEAD_CHARS = 600
TITLE_CHARS = 100

_INID = re.compile(
    r"[(（]\s*(?:1[0-9]|2[0-9]|4[0-9]|5[0-9]|6[0-9]|7[0-9]|8[0-9])\s*[)）]\s*"
    r"(?:中华人民共和国|国家知识产权局|发明专利|实用新型|外观设计|申请公布号|授权公告号|申请号|申请日|"
    r"申请公布日|授权公告日|申请人|发明人|设计人|专利权人|发明名称|实用新型名称|使用外观设计的产品名称|"
    r"摘要|国际专利分类号|优先权数据|专利代理机构|代理人|地址)"
)
_FIELD = re.compile(
    r"(申请公布号|授权公告号|公开号|公告号|申请号|专利号)\s*[:：]?\s*(?:CN|ZL)?\s*\d{6,}", re.I
)
_BARE_NO = re.compile(r"(?<![A-Za-z0-9])(?:CN\s*\d{8,}(?:\.\d)?\s*[A-Z]\d?|ZL\s*\d{8,}(?:\.[\dX])?)", re.I)
_OFFICE = ("国家知识产权局", "专利局", "知识产权局")
_TYPE = ("发明专利", "实用新型专利", "外观设计专利", "专利说明书", "权利要求书", "专利申请公布", "专利证书")
_INVALID = {"ZERO_BYTES", "ZERO_PAGES", "CORRUPT"}


def patent_status(filename: str, text: str, *, text_missing: bool) -> str:
    """返回 approved、review 或 pass。pass 表示可以继续往下看，不是「已经证明不是专利」。"""
    head = text[:HEAD_CHARS]
    title = text[:TITLE_CHARS]
    inid = len(_INID.findall(head))
    fields = len(_FIELD.findall(head))
    office = any(word in head for word in _OFFICE)
    typed = any(word in head for word in _TYPE)
    name_no = bool(_BARE_NO.search(filename))
    if inid >= 2 or (office and (fields or typed)) or (name_no and (inid or office or fields)):
        return "approved"
    if text_missing or not text.strip():
        return "review"
    weak = (
        inid or fields or name_no or "专利" in filename
        or bool(_BARE_NO.search(title)) or any(word in title for word in _TYPE)
    )
    return "review" if weak else "pass"


def is_invalid(error_code: str) -> bool:
    return error_code in _INVALID
