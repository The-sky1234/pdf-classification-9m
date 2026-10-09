import pymupdf as fitz


def make_text_pdf(path: str, title: str = "Safety Inspection Notice", body: str = "Section one on major hazard inspection.", pages: int = 1):
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 100), title if i == 0 else "章节二", fontsize=22)
        page.insert_text((72, 160), f"{body} 第{i + 1}页。", fontsize=11)
    doc.save(path)
    doc.close()


def make_blank_image_pdf(path: str, pages: int = 1):
    import io
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page()
        pix = page.get_pixmap(dpi=72)
        png = pix.tobytes("png")
        page2 = doc.new_page()
        page2.insert_image(page2.rect, stream=png)
    doc.save(path)
    doc.close()


def fake_ocr_backend(lines=("OCR标题", "OCR正文段落")):
    def _fn(image_bytes: bytes):
        out = []
        y = 10.0
        for t in lines:
            out.append((t, (10.0, y, 200.0, y + 20.0), 0.95))
            y += 30.0
        return out
    return _fn
