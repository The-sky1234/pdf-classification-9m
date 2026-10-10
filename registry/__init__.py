"""文件登记：UUIDv7 身份、规范路径、重复扫描幂等。"""
from registry.service import (
    DEFAULT_FACTORY,
    RegisterOutcome,
    BatchStateError,
    RetryableScanError,
    ScanBusy,
    ScanSummary,
    register_candidates,
    register_file,
    reopen_failed_batch,
    reregister,
    scan_batch,
)
from registry.uuidv7 import UuidV7Factory

__all__ = [
    "DEFAULT_FACTORY",
    "BatchStateError",
    "RegisterOutcome",
    "RetryableScanError",
    "ScanBusy",
    "ScanSummary",
    "UuidV7Factory",
    "register_candidates",
    "register_file",
    "reopen_failed_batch",
    "reregister",
    "scan_batch",
]
