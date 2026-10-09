from __future__ import annotations
from parser.models import PdfType, EvidenceMode
from parser.mineru_adapter import MinerUAdapter

TYPE_TO_BACKEND = {PdfType.TEXT_PDF: "pymupdf", PdfType.SCAN_PDF: "paddleocr",
                   PdfType.MIXED_PDF: "pymupdf+paddleocr", PdfType.COMPLEX_PDF: "mineru-on-demand",
                   PdfType.IMAGE_PDF: "paddleocr"}
TYPE_TO_EVIDENCE = {PdfType.TEXT_PDF: EvidenceMode.TITLE, PdfType.SCAN_PDF: EvidenceMode.OCR_BODY,
                    PdfType.MIXED_PDF: EvidenceMode.BODY, PdfType.COMPLEX_PDF: EvidenceMode.BODY,
                    PdfType.IMAGE_PDF: EvidenceMode.OCR_BODY}


def route(pdf_type: str, has_title: bool = False) -> dict:
    if pdf_type == PdfType.COMPLEX_PDF and not MinerUAdapter.allowed(pdf_type):
        raise AssertionError("mineru must only serve complex/mixed")
    evidence = TYPE_TO_EVIDENCE[pdf_type]
    if pdf_type in (PdfType.TEXT_PDF, PdfType.MIXED_PDF) and not has_title:
        evidence = EvidenceMode.BODY
    return {"backend": TYPE_TO_BACKEND[pdf_type], "evidence_mode": evidence}
