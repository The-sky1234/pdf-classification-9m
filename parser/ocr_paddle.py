"""PaddleOCR 2.x 封装（deploy/requirements-ocr.txt 固定 paddleocr==2.8.1）。

结果格式不认识就抛异常，不静默丢行：识别结果少一半却当成功，比失败更难发现。
"""
from __future__ import annotations

import os
import threading


class OcrFormatError(RuntimeError):
    pass


class PaddleOcrEngine:
    model_version = "paddleocr-2.8-ch"

    def __init__(self, use_gpu: bool | None = None, lang: str = "ch"):
        if use_gpu is None:
            use_gpu = os.environ.get("OCR_USE_GPU", "1") not in ("0", "false", "no")
        self.use_gpu = use_gpu
        self.lang = lang
        self._ocr = None
        self._lock = threading.Lock()

    def _load(self):
        if self._ocr is None:
            from paddleocr import PaddleOCR  # type: ignore

            self._ocr = PaddleOCR(lang=self.lang, use_angle_cls=True, use_gpu=self.use_gpu, show_log=False)
        return self._ocr

    def warmup(self) -> None:
        import numpy as np

        self.recognize(np.full((64, 256, 3), 255, dtype=np.uint8))

    def recognize(self, image) -> list[tuple[str, float]]:
        """image 是 HxWx3 的 BGR uint8 数组。返回 [(一行文字, 置信度)]，按版面从上到下。"""
        with self._lock:
            raw = self._load().ocr(image, cls=True)
        return parse_paddle_result(raw)


def parse_paddle_result(raw) -> list[tuple[str, float]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise OcrFormatError(f"PaddleOCR 返回了 {type(raw).__name__}，不是 list")
    lines: list[tuple[str, float]] = []
    for page in raw:
        if page is None:
            continue
        if not isinstance(page, list):
            raise OcrFormatError(f"PaddleOCR 页结果是 {type(page).__name__}，不是 list（版本不对？）")
        for item in page:
            try:
                _box, (text, score) = item[0], item[1]
            except (TypeError, ValueError, IndexError) as exc:
                raise OcrFormatError(f"PaddleOCR 行结果格式不认识：{item!r:.200}") from exc
            lines.append((str(text), float(score)))
    return lines


def paddle_ocr_backend(image_bytes: bytes) -> list[tuple[str, tuple, float]]:
    """旧接口：PNG/JPEG 字节进，[(文字, (0,0,0,0), 置信度)] 出。"""
    import numpy as np
    import pymupdf

    pix = pymupdf.Pixmap(image_bytes)
    if pix.n not in (3,):
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, ::-1].copy()
    if not hasattr(paddle_ocr_backend, "_engine"):
        paddle_ocr_backend._engine = PaddleOcrEngine()
    return [(text, (0, 0, 0, 0), score) for text, score in paddle_ocr_backend._engine.recognize(arr)]
