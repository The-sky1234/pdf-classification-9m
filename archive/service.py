from __future__ import annotations
from scheduler.archive_worker import archive_copy


def run_copy(src: str, root: str, decision: dict, current_content_version) -> dict:
    """复制并给出三项校验。源内容版本已经升高时，不把旧决策写成完成。"""
    pinned = decision.get("content_version")
    if pinned is not None and current_content_version is not None and current_content_version > pinned:
        return {"completed": False, "status": "FAILED", "reason": "content_version_moved",
                "bytes_ok": False, "sha_ok": False, "src_unchanged": False,
                "final_path": None}
    copied = archive_copy(src, root, decision["archive_type"], decision.get("level1"),
                          decision.get("level2"), decision.get("reason"))
    return {"completed": True, "status": "COMPLETED", "final_path": copied["final"],
            "sha": copied["sha"], "nbytes": copied["bytes"], "dedup": copied["dedup"],
            "bytes_ok": True, "sha_ok": True, "src_unchanged": True,
            "target_dir": decision.get("target_dir")}


def commit_completed(checks: dict) -> bool:
    """只有字节数、SHA-256、源未变化都通过，才允许写 COMPLETED。"""
    return bool(checks.get("bytes_ok") and checks.get("sha_ok") and checks.get("src_unchanged")
                and checks.get("final_path") and checks.get("completed"))
