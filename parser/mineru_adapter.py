from __future__ import annotations
from parser.models import PageContent
from parser.errors import ParseError, ErrorCode


class MinerUAdapter:
    name = "mineru"

    def parse(self, file_path: str, page_nos: list[int] | None = None) -> list[PageContent]:
        try:
            from magic_pdf.data.data_reader_writer import FileBasedDataWriter, FileBasedDataReader
            from magic_pdf.api import magic_pdf_parse
        except ImportError as e:
            raise ParseError(ErrorCode.MINERU_FAILED, f"mineru not installed: {e}", retryable=False)
        raise ParseError(ErrorCode.MINERU_FAILED, "mineru backend wiring intentionally minimal in phase-2",
                         retryable=False)

    @staticmethod
    def allowed(pdf_type: str) -> bool:
        return pdf_type in ("COMPLEX_PDF", "MIXED_PDF")
