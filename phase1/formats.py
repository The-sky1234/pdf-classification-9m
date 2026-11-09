"""扩展名分组。分组决定波次 0 用不用 GPU，不决定业务目录。"""
from __future__ import annotations

import os

PDF = {".pdf"}
OOXML = {".docx", ".docm", ".xlsx", ".xlsm", ".pptx", ".pptm"}
PLAIN = {".txt", ".md", ".log", ".csv", ".json", ".html", ".htm", ".xml", ".rtf"}
IMAGE = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp"}
# 没有批准的提取器。抽不出文字就待复核，不送 MinerU。
NO_EXTRACTOR = {".doc", ".wps", ".xls", ".et", ".ppt", ".dps", ".ofd"}
# 打开会改变「700 万主件」边界，或本阶段不解析正文。
CLOSED = {
    ".zip", ".rar", ".7z", ".tar", ".gz",
    ".dwg", ".dxf",
    ".mp4", ".wmv", ".rmvb", ".mp3",
    ".mdb", ".accdb", ".db", ".sqlite",
}


def extension_of(path: str) -> str:
    return os.path.splitext(path)[1].lower()
