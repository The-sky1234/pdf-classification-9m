from __future__ import annotations
import os

# 登记不再只收 PDF。办公文档、网页、版式文件和图片都进入同一条分类链路。
DOCUMENT_EXTENSIONS = {
    ".pdf", ".ofd",
    ".doc", ".docx", ".docm", ".wps", ".rtf",
    ".xls", ".xlsx", ".xlsm", ".et", ".csv",
    ".ppt", ".pptx", ".pptm", ".dps",
    ".txt", ".md", ".log", ".json", ".xml", ".html", ".htm",
    ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp",
}


def accepted_extensions() -> set[str]:
    extra = os.environ.get("DOCUMENT_EXTENSIONS", "")
    if extra.strip() == "*":
        return {"*"}
    found = set(DOCUMENT_EXTENSIONS)
    for part in extra.split(","):
        ext = part.strip().lower()
        if not ext:
            continue
        if not ext.startswith("."):
            ext = "." + ext
        found.add(ext)
    return found


def is_document(name: str) -> bool:
    if not name or name.startswith("."):
        return False
    allowed = accepted_extensions()
    if allowed == {"*"}:
        return True
    return os.path.splitext(name)[1].lower() in allowed
