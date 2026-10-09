from __future__ import annotations


class VisionStub:
    name = "vision-reserved"

    def describe(self, image_bytes: bytes) -> dict:
        raise NotImplementedError("vision model reserved for later phase; OCR path must be tried first")
