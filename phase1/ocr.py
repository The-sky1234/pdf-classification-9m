"""波次 1：只识别扫描页和图片，拿到的文字交回 judge，再进分类波次。"""
from __future__ import annotations

import os

from phase1.extract import MAX_TEXT, PDF_SAMPLE_PAGES, Extract
from phase1.formats import IMAGE, extension_of

OCR_DPI = int(os.environ.get("OCR_DPI", "200"))
# 图纸、长图按最长边限像素，避免一页几亿像素把显存撑爆。
OCR_MAX_SIDE = int(os.environ.get("OCR_MAX_SIDE", "4000"))


class OcrUnreadable(Exception):
    """文件能读，但渲染不出图像（例如 MuPDF 不支持的图片编码）。不是损坏证据，去待复核。"""


def render_pages(path: str, pages: list[int] | None):
    """按页渲染成 BGR uint8 数组。pages 为空表示整份（图片文件），只取前 PDF_SAMPLE_PAGES 页。"""
    import numpy as np
    import pymupdf

    try:
        doc = pymupdf.open(path)
    except Exception as exc:
        raise OcrUnreadable(str(exc)) from exc
    try:
        wanted = pages if pages else list(range(min(doc.page_count, PDF_SAMPLE_PAGES)))
        for index in wanted:
            if index >= doc.page_count:
                continue
            page = doc[index]
            longest = max(page.rect.width, page.rect.height) or 1.0
            dpi = max(36, min(OCR_DPI, int(OCR_MAX_SIDE * 72 / longest)))
            pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
            arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
            yield index, arr[:, :, ::-1].copy()
    finally:
        doc.close()


def ocr_file(path: str, text_layer: str, pages: list[int], engine) -> Extract:
    """返回一个新的 Extract，outcome=text，error_code=OCR。text_layer 是波次 0 已经拿到的文字页内容。"""
    ext = extension_of(path)
    if ext in IMAGE:
        pages = []
    parts: list[str] = []
    scores: list[float] = []
    for _index, image in render_pages(path, pages):
        lines = engine.recognize(image)
        parts.append("\n".join(text for text, _ in lines))
        scores.extend(score for _, score in lines)
    ocr_text = "\n".join(p for p in parts if p.strip()).strip()
    # 扫描页通常在前（封面、首页），OCR 文字放前面，专利著录项和标题才会落在开头。
    text = "\n".join(p for p in (ocr_text, text_layer) if p and p.strip())[:MAX_TEXT]
    got = Extract(path, ext, text=text, outcome="text", error_code="OCR")
    got.message = f"ocr_lines={len(scores)} mean_score={sum(scores) / len(scores):.3f}" if scores else "ocr_lines=0"
    return got
