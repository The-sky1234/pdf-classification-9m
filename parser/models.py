from __future__ import annotations
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Optional


class PdfType(str, Enum):
    TEXT_PDF = "TEXT_PDF"
    SCAN_PDF = "SCAN_PDF"
    MIXED_PDF = "MIXED_PDF"
    COMPLEX_PDF = "COMPLEX_PDF"
    IMAGE_PDF = "IMAGE_PDF"


class EvidenceMode(str, Enum):
    TITLE = "TITLE"
    BODY = "BODY"
    OCR_BODY = "OCR_BODY"
    VISION = "VISION"


class ErrorCode(str, Enum):
    RETRYABLE_IO = "RETRYABLE_IO"
    CORRUPT = "CORRUPT_UNRECOVERABLE"
    ENCRYPTED = "ENCRYPTED_UNREADABLE"
    ZERO_PAGES = "ZERO_PAGES"
    OCR_FAILED = "OCR_FAILED"
    MINERU_FAILED = "MINERU_FAILED"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass
class BBox:
    x0: float
    y0: float
    x1: float
    y1: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Span:
    text: str
    bbox: BBox
    size: float = 0.0
    font: str = ""
    page_no: int = 1
    ocr_score: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PageContent:
    page_no: int
    text: str
    spans: list = field(default_factory=list)
    image_count: int = 0
    char_count: int = 0
    ocr_score: Optional[float] = None

    def to_dict(self) -> dict:
        return {"page_no": self.page_no, "text": self.text,
                "spans": [s.to_dict() if hasattr(s, "to_dict") else s for s in self.spans],
                "image_count": self.image_count, "char_count": self.char_count,
                "ocr_score": self.ocr_score}


@dataclass
class ParseErrorInfo:
    code: str
    message: str
    retryable: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ParseResult:
    file_path: str
    pdf_type: Optional[str] = None
    evidence_mode: Optional[str] = None
    page_count: int = 0
    title: str = ""
    headings: list = field(default_factory=list)
    pages: list = field(default_factory=list)
    ocr_mean_score: Optional[float] = None
    ok: bool = True
    error: Optional[ParseErrorInfo] = None
    backend: str = ""
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"file_path": self.file_path, "pdf_type": self.pdf_type,
                "evidence_mode": self.evidence_mode, "page_count": self.page_count,
                "title": self.title, "headings": self.headings,
                "pages": [p.to_dict() if hasattr(p, "to_dict") else p for p in self.pages],
                "ocr_mean_score": self.ocr_mean_score, "ok": self.ok,
                "error": self.error.to_dict() if self.error else None,
                "backend": self.backend, "truncated": self.truncated}
