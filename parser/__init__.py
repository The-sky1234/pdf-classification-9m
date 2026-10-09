"""PDF解析模块：只读原文件，按类型分流提取统一结构化结果。"""
from parser.models import BBox, PageContent, ParseResult, EvidenceMode, PdfType, ParseErrorInfo
from parser.parser import parse_pdf, detect_only
from parser.router import route

__all__ = ["parse_pdf", "detect_only", "route", "ParseResult", "PageContent", "BBox", "EvidenceMode", "PdfType", "ParseErrorInfo"]
