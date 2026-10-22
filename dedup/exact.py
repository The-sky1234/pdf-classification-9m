from __future__ import annotations
import re
from sqlalchemy import select
from sqlalchemy.orm import Session
from backend.models import DedupMember
from registry.ids import uuid7

_SHA = re.compile(r"^[0-9a-f]{64}$")


def exact(db: Session, file_id: str, source_sha256: str, nbytes: int) -> dict:
    """文件字节 SHA-256 的精确重复关系。不返回归档路径，不认定近重复。"""
    if not _SHA.fullmatch(source_sha256 or "") or nbytes is None or nbytes < 0:
        raise ValueError("sha incomplete")
    mine = db.get(DedupMember, file_id)
    if mine:
        return {"group_id": mine.group_id, "is_exact_dup": bool(mine.is_exact_dup),
                "master_id": mine.master_id}
    prior = db.scalar(select(DedupMember).where(
        DedupMember.source_sha256 == source_sha256).order_by(DedupMember.file_id))
    if prior is None:
        group = uuid7()
        db.add(DedupMember(file_id=file_id, source_sha256=source_sha256, nbytes=nbytes,
                           group_id=group, master_id=file_id, is_exact_dup=0))
        db.commit()
        return {"group_id": group, "is_exact_dup": False, "master_id": file_id}
    db.add(DedupMember(file_id=file_id, source_sha256=source_sha256, nbytes=nbytes,
                       group_id=prior.group_id, master_id=prior.master_id, is_exact_dup=1))
    db.commit()
    return {"group_id": prior.group_id, "is_exact_dup": True, "master_id": prior.master_id}
