"""旧入口，保留函数签名。复制逻辑统一走 phase1.archive，不再整文件读进内存，也不会覆盖已有文件。"""
from __future__ import annotations

from classifier.taxonomy_v3 import target_dir
from phase1.archive import copy_into


def archive_copy(src, root, atype, l1=None, l2=None, reason=None):
    rel = target_dir(atype, None, l1, l2, reason)
    out = copy_into(src, root, rel)
    return {"final": out["final"], "sha": out["sha256"], "bytes": out["nbytes"], "dedup": out["existed"]}
