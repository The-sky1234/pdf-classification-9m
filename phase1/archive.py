"""按已经定下来的结论复制原文件。文件名保留原扩展名。

先把字节写到同目录的临时文件，大小和 SHA-256 都与源文件一致，再改成正式文件名。
"""
from __future__ import annotations

import hashlib
import os
import uuid

from phase1.judge import Decision

_CHUNK = 1024 * 1024


def copy_settled(src: str, archive_root: str, decision: Decision) -> dict:
    """只接受 settled 的结论。源文件在归档根里面时拒绝，避免以后把副本再扫进来。"""
    if not decision.settled or not decision.target_dir:
        raise ValueError("结论还没定，不能复制")
    if not os.path.isfile(src):
        raise FileNotFoundError(src)
    src_real = os.path.realpath(src)
    root_real = os.path.realpath(archive_root)
    if _is_inside(src_real, root_real):
        raise ValueError("源文件在归档根里面")
    dest_dir = os.path.realpath(os.path.join(root_real, decision.target_dir))
    if not _is_inside(dest_dir, root_real) and dest_dir != root_real:
        raise ValueError("目标目录跳出了归档根")
    os.makedirs(dest_dir, exist_ok=True)
    src_sha, src_size = _hash_file(src)
    final_name = _safe_name(src_sha, src)
    final_path = os.path.join(dest_dir, final_name)
    if os.path.isfile(final_path):
        old_sha, old_size = _hash_file(final_path)
        if old_sha == src_sha and old_size == src_size:
            return {"final": final_path, "sha256": src_sha, "nbytes": src_size, "existed": True}
    tmp_path = os.path.join(dest_dir, f".part-{uuid.uuid4().hex}")
    try:
        _copy_bytes(src, tmp_path)
        tmp_sha, tmp_size = _hash_file(tmp_path)
        if tmp_sha != src_sha or tmp_size != src_size:
            raise ValueError("复制后的大小或 SHA-256 与源文件不一致")
        os.replace(tmp_path, final_path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    return {"final": final_path, "sha256": src_sha, "nbytes": src_size, "existed": False}


def _copy_bytes(src: str, dest: str) -> None:
    with open(src, "rb") as incoming, open(dest, "wb") as outgoing:
        while True:
            blob = incoming.read(_CHUNK)
            if not blob:
                break
            outgoing.write(blob)


def _hash_file(path: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            blob = handle.read(_CHUNK)
            if not blob:
                break
            digest.update(blob)
            size += len(blob)
    return digest.hexdigest(), size


def _safe_name(sha: str, src: str) -> str:
    base = os.path.basename(src)
    stem, ext = os.path.splitext(base)
    ext = "".join(ch for ch in ext.lower() if ch.isalnum() or ch == ".")[:16]
    clean = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in stem)[:60] or "file"
    return f"{sha[:12]}_{clean}{ext}"


def _is_inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False
