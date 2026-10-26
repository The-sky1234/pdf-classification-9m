from __future__ import annotations
import json
import os
import time
from collections import Counter
from sqlalchemy import select
from sqlalchemy.orm import Session
from backend.models import ArchiveDecision, ArchiveExecution, FileRegistry
from classifier.taxonomy_v3 import L1, L2
from scheduler.benchmark import Sample, estimate_full_run
from scheduler.scan_classify import run as run_batch

# 技术实施里的六个阶段。知识库核对、混合样本验证、阈值对照、压测估算、试运行比率、全量续跑。
PHASES = ("1_knowledge", "2_validate", "3_calibrate", "4_stress", "5_trial", "6_full")


def run_campaign(db: Session, root: str, archive_root: str, *, limit: int = 200,
                 batch_id: int | None = None, labels_path: str | None = None,
                 total_files: int = 9_800_000, calibrated: bool | None = None) -> dict:
    """对一个混合目录连续做完六个阶段，并返回一份报告。

    没有标注文件时，校准阶段只记录「未签署」，不打开自动归档。
    """
    knowledge = _knowledge()
    started = time.perf_counter()
    summary = run_batch(db, root, archive_root, limit=limit, batch_id=batch_id,
                        calibrated=calibrated)
    elapsed = max(time.perf_counter() - started, 1e-6)
    files = _files(db)
    by_ext = Counter(item["ext"] for item in files)
    missing = [item["source_path"] for item in files if item["status"] is None]
    validate = {
        "by_ext": dict(sorted(by_ext.items())),
        "files": len(files),
        "missing_decisions": len(missing),
        "kinds_seen": sorted({item["ext"] for item in files}),
        "passed": len(files) > 0 and not missing,
    }
    calibrate = _calibrate(files, labels_path)
    pages = sum(1 for item in files if item["ext"] != "png")
    estimate = estimate_full_run(Sample(len(files), max(pages, len(files)), elapsed), total_files)
    by_type = Counter(item["archive_type"] or "NONE" for item in files)
    review = by_type.get("PENDING", 0)
    trial = {
        "by_archive_type": dict(sorted(by_type.items())),
        "review_rate": round(review / len(files), 4) if files else None,
        "completed": sum(1 for item in files if item["status"] == "COMPLETED"),
    }
    return {
        "phases": {
            "1_knowledge": knowledge,
            "2_validate": validate,
            "3_calibrate": calibrate,
            "4_stress": {"elapsed_sec": round(elapsed, 4), "estimate": estimate,
                         "sample_files": len(files)},
            "5_trial": trial,
            "6_full": {"batch_id": summary["batch_id"], "counts": summary["counts"],
                       "resume_batch_id": summary["batch_id"]},
        }
    }


def _knowledge() -> dict:
    counts = {key: len(names) for key, names in L2.items()}
    return {"taxonomy_version": "v3.0-phase1", "level1": list(L1), "l2_counts": counts,
            "total_l2": sum(counts.values()), "ready": sum(counts.values()) == 27 and len(L1) == 4}


def _files(db: Session) -> list[dict]:
    rows = []
    regs = db.scalars(select(FileRegistry)).all()
    for reg in regs:
        decision = db.scalar(select(ArchiveDecision).where(ArchiveDecision.file_id == reg.file_id)
                             .order_by(ArchiveDecision.version.desc()))
        execution = None
        if decision is not None:
            execution = db.scalar(select(ArchiveExecution).where(
                ArchiveExecution.decision_id == decision.id).order_by(ArchiveExecution.id.desc()))
        ext = os.path.splitext(reg.source_path)[1].lower().lstrip(".") or "none"
        rows.append({
            "file_id": reg.file_id,
            "source_path": reg.source_path,
            "ext": ext,
            "archive_type": decision.archive_type if decision else None,
            "level1": decision.level1 if decision else None,
            "level2": decision.level2 if decision else None,
            "reason": decision.reason if decision else None,
            "status": execution.status if execution else None,
            "final_path": execution.final_path if execution else None,
        })
    return rows


def _calibrate(files: list[dict], labels_path: str | None) -> dict:
    if not labels_path:
        return {"status": "withheld", "compared": 0, "correct": 0,
                "thresholds_applied": False,
                "reason": "没有标注文件，自动通过阈值保持关闭"}
    labels = json.loads(open(labels_path, encoding="utf-8").read())
    by_name = {os.path.basename(item["source_path"]): item for item in files}
    compared = correct = 0
    for label in labels:
        name = os.path.basename(label.get("source_path") or label.get("name") or "")
        got = by_name.get(name)
        if got is None:
            continue
        compared += 1
        expect_l2 = label.get("level2")
        expect_reason = label.get("reason")
        if expect_l2 and got.get("level2") == expect_l2:
            correct += 1
        elif expect_reason and got.get("reason") == expect_reason:
            correct += 1
    return {"status": "scored", "compared": compared, "correct": correct,
            "thresholds_applied": False,
            "reason": "对照结果只供签署，本程序不打开 THRESHOLDS_CALIBRATED"}
