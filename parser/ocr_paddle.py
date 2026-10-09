from __future__ import annotations


def paddle_ocr_backend(image_bytes: bytes) -> list[tuple[str, tuple, float]]:
    from paddleocr import PaddleOCR
    import tempfile, os
    if not hasattr(paddle_ocr_backend, "_ocr"):
        paddle_ocr_backend._ocr = PaddleOCR(lang="ch")
    ocr = paddle_ocr_backend._ocr
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(image_bytes)
        path = f.name
    try:
        raw = ocr.ocr(path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    lines: list[tuple[str, tuple, float]] = []
    for page in raw or []:
        for item in page or []:
            try:
                box, (text, score) = item[0], item[1]
                xs = [p[0] for p in box]
                ys = [p[1] for p in box]
                lines.append((str(text), (min(xs), min(ys), max(xs), max(ys)), float(score)))
            except Exception:
                continue
    return lines
