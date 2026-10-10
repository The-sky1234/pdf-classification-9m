"""规范路径。生产环境是 Linux，路径区分大小写。"""
from __future__ import annotations

import os
import posixpath
import stat
from datetime import datetime, timezone

SURROGATE_MIN = 0xDC80
SURROGATE_MAX = 0xDCFF


class PathRejected(Exception):
    def __init__(self, reason: str, lexical: str | None, advance_cursor: bool):
        super().__init__(reason)
        self.reason = reason
        self.lexical = lexical
        self.advance_cursor = advance_cursor


class LstatFailed(Exception):
    def __init__(self, lexical: str):
        super().__init__(lexical)
        self.lexical = lexical


def _has_surrogate(path: str) -> bool:
    return any(SURROGATE_MIN <= ord(ch) <= SURROGATE_MAX for ch in path)


def _norm_absolute(path: str) -> str:
    if not isinstance(path, str) or path == "":
        raise PathRejected("empty", None, False)
    if "\x00" in path:
        raise PathRejected("nul", None, False)
    # 保留大小写，不做 NFC，不把反斜杠换成斜杠。
    norm = posixpath.normpath(path)
    # normpath 会留下恰好两个前导斜杠。Linux 上它和单斜杠是同一路径。
    if norm.startswith("//"):
        norm = "/" + norm[2:]
    if not norm.startswith("/") or norm == "/" or norm.endswith("/"):
        raise PathRejected("not_absolute", None, False)
    return norm


def canonical_root(path: str) -> str:
    if _has_surrogate(path) if isinstance(path, str) else False:
        raise PathRejected("surrogate_root", None, False)
    return _norm_absolute(path)


def lexical_candidate(path: str, roots: tuple[str, ...]) -> str:
    """步骤 1–7。通过则返回规范路径；拒绝时抛 PathRejected。"""
    surrogate = isinstance(path, str) and _has_surrogate(path)
    try:
        norm = _norm_absolute(path)
    except PathRejected:
        raise
    if surrogate:
        advance = _under_root(norm, roots)
        raise PathRejected("surrogate", norm if advance else None, advance)
    if not _under_root(norm, roots):
        raise PathRejected("outside_root", norm, False)
    return norm


def _under_root(canon: str, roots: tuple[str, ...]) -> bool:
    return any(canon.startswith(root + "/") for root in roots)


def lstat_regular(canon: str):
    try:
        st = os.lstat(canon)
    except OSError as exc:
        raise LstatFailed(canon) from exc
    if stat.S_ISLNK(st.st_mode):
        raise PathRejected("symlink", canon, True)
    if not stat.S_ISREG(st.st_mode):
        raise PathRejected("not_regular", canon, True)
    return st


def mtime_utc(st) -> datetime:
    micros = st.st_mtime_ns // 1000
    seconds, micro = divmod(micros, 1_000_000)
    return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(microsecond=micro)


def path_sort_key(path: str) -> bytes:
    """与 PostgreSQL COLLATE \"C\" 的 UTF-8 字节序一致。"""
    return path.encode("utf-8", "surrogateescape")


def path_lt(left: str, right: str) -> bool:
    return path_sort_key(left) < path_sort_key(right)


def iter_pdfs(root: str) -> tuple[list[str], list[tuple[str, str]]]:
    """返回 PDF 路径，以及 ``(目录, 原因)`` 形式的列举失败。

    目录符号链接仍然不进入。列举失败只记录，不当成空目录。
    """
    found: list[str] = []
    errors: list[tuple[str, str]] = []

    def onerror(err: OSError) -> None:
        errors.append((err.filename or root, err.strerror or str(err)))

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=onerror):
        dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
        for name in filenames:
            if name.lower().endswith(".pdf"):
                found.append(os.path.join(dirpath, name))
    return found, errors
