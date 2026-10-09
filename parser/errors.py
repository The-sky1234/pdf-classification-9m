from __future__ import annotations
from parser.models import ParseErrorInfo, ErrorCode


class ParseError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.info = ParseErrorInfo(code=code, message=message, retryable=retryable)


CORRUPT_HINTS = ("damaged", "corrupt", "invalid", "cannot open", "failed to open")


def classify_open_exception(exc: Exception) -> ParseError:
    msg = str(exc).lower()
    if "password" in msg or "encrypt" in msg:
        return ParseError(ErrorCode.ENCRYPTED, f"encrypted/unreadable: {exc}", retryable=False)
    if any(h in msg for h in CORRUPT_HINTS) or "no pages" in msg:
        return ParseError(ErrorCode.CORRUPT, f"corrupt pdf: {exc}", retryable=False)
    if isinstance(exc, FileNotFoundError):
        return ParseError(ErrorCode.CORRUPT, f"file not found: {exc}", retryable=False)
    if isinstance(exc, (OSError, IOError, TimeoutError)):
        return ParseError(ErrorCode.RETRYABLE_IO, f"transient io: {exc}", retryable=True)
    return ParseError(ErrorCode.CORRUPT, f"unrecoverable: {exc}", retryable=False)
