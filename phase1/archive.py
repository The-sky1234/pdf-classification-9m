"""按已经定下来的结论复制原文件。文件名保留原扩展名，源文件只读。

1. 边读源文件边算 SHA-256，写进同目录的临时文件并 fsync；
2. 重新读临时文件核对大小和 SHA-256，并确认源文件在复制期间没有变过；
3. 用硬链接把临时文件放到正式文件名上：目标已存在时绝不覆盖（AR-008）。
   同名且内容相同算已归档；同名不同内容就加长哈希前缀换个名字。
"""
from __future__ import annotations

import errno
import hashlib
import os
import uuid

_CHUNK = 1024 * 1024
_NAME_STEM_MAX = 60


class ArchiveError(Exception):
    pass


def copy_settled(src: str, archive_root: str, decision) -> dict:
    """只接受 settled 的结论。decision 可以是 Decision，也可以是带 settled/target_dir 的 dict。"""
    settled = _get(decision, "settled")
    rel = _get(decision, "target_dir")
    if not settled or not rel:
        raise ValueError("结论还没定，不能复制")
    return copy_into(src, archive_root, rel)


def copy_into(src: str, archive_root: str, rel_dir: str) -> dict:
    if not os.path.isfile(src):
        raise FileNotFoundError(src)
    root_real = os.path.realpath(archive_root)
    if _is_inside(os.path.realpath(src), root_real):
        raise ValueError("源文件在归档根里面")
    dest_dir = os.path.realpath(os.path.join(root_real, rel_dir))
    if not _is_inside(dest_dir, root_real):
        raise ValueError("目标目录跳出了归档根")
    os.makedirs(dest_dir, exist_ok=True)
    before = os.stat(src)
    tmp_path = os.path.join(dest_dir, f".part-{uuid.uuid4().hex}")
    try:
        src_sha, src_size = _copy_hashing(src, tmp_path)
        after = os.stat(src)
        if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns) or after.st_size != src_size:
            raise ArchiveError("复制期间源文件发生了变化")
        tmp_sha, tmp_size = hash_file(tmp_path)
        if (tmp_sha, tmp_size) != (src_sha, src_size):
            raise ArchiveError("复制后的大小或 SHA-256 与源文件不一致")
        final_path, existed = _place(tmp_path, dest_dir, src, src_sha, src_size)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    return {"final": final_path, "sha256": src_sha, "nbytes": src_size, "existed": existed}


def remove_copy(path: str, archive_root: str, sha256: str) -> bool:
    """复核改判后删掉旧位置的副本（AR-010）。只删归档根里、内容与记录一致的文件。"""
    root_real = os.path.realpath(archive_root)
    real = os.path.realpath(path)
    if not _is_inside(real, root_real) or real == root_real:
        raise ValueError("只删归档根里的副本")
    if not os.path.isfile(real):
        return False
    if hash_file(real)[0] != sha256:
        raise ArchiveError("旧副本内容与记录不一致，不删")
    os.remove(real)
    return True


def _place(tmp_path: str, dest_dir: str, src: str, sha: str, size: int) -> tuple[str, bool]:
    for width in (12, 20, 32, 64):
        final_path = os.path.join(dest_dir, safe_name(sha, src, width))
        try:
            os.link(tmp_path, final_path)
            return final_path, False
        except FileExistsError:
            if hash_file(final_path) == (sha, size):
                return final_path, True
        except OSError as exc:
            if exc.errno not in (errno.EPERM, errno.EOPNOTSUPP, errno.EXDEV, errno.ENOTSUP):
                raise
            if _exclusive_rename(tmp_path, final_path):
                return final_path, False
            if hash_file(final_path) == (sha, size):
                return final_path, True
    raise ArchiveError(f"目标目录里同名文件过多，拒绝覆盖：{dest_dir}")


def _exclusive_rename(tmp_path: str, final_path: str) -> bool:
    """文件系统不支持硬链接时的退路：O_EXCL 先占住名字，再改名覆盖这个自己刚建的空文件。"""
    try:
        fd = os.open(final_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    os.close(fd)
    os.replace(tmp_path, final_path)
    return True


def _copy_hashing(src: str, dest: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(src, "rb") as incoming, open(dest, "xb") as outgoing:
        while True:
            blob = incoming.read(_CHUNK)
            if not blob:
                break
            digest.update(blob)
            size += len(blob)
            outgoing.write(blob)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    return digest.hexdigest(), size


def hash_file(path: str) -> tuple[str, int]:
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


def safe_name(sha: str, src: str, width: int = 12) -> str:
    base = os.path.basename(src.replace("\\", "/"))
    stem, ext = os.path.splitext(base)
    ext = "".join(ch for ch in ext.lower() if ch.isalnum() or ch == ".")[:16]
    clean = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in stem)[:_NAME_STEM_MAX] or "file"
    return f"{sha[:width]}_{clean}{ext}"


def _get(obj, key):
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key)


def _is_inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False
