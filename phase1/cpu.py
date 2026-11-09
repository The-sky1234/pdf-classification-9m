"""单机试跑：不连数据库，只跑波次 0，把结论写进 jsonl。

服务器上的正式流程用台账（python -m phase1 load / work），这里给开发机和抽样核对用。
单个文件出任何错都只记一行 error，不中断整批；结果先写临时文件，跑完再改名。
"""
from __future__ import annotations

import json
import os

from phase1.archive import copy_settled
from phase1.judge import judge_file
from phase1.masters import iter_master_paths


def run_cpu_wave(masters_path: str, out_jsonl: str, archive_root: str | None = None) -> dict:
    counts = {"settled": 0, "ocr": 0, "classify": 0, "retry": 0, "archived": 0, "missing": 0, "error": 0}
    os.makedirs(os.path.dirname(os.path.abspath(out_jsonl)) or ".", exist_ok=True)
    part = out_jsonl + ".part"
    with open(part, "w", encoding="utf-8") as out:
        for path in iter_master_paths(masters_path):
            if not os.path.isfile(path):
                counts["missing"] += 1
                out.write(json.dumps({"source_path": path, "reason": "missing"}, ensure_ascii=False) + "\n")
                continue
            try:
                decision = judge_file(path)
                record = decision.to_dict()
                if decision.settled and archive_root:
                    record["archive"] = copy_settled(path, archive_root, decision)
                    counts["archived"] += 1
            except Exception as exc:  # noqa: BLE001
                counts["error"] += 1
                out.write(json.dumps({"source_path": path, "reason": "error",
                                      "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False) + "\n")
                continue
            if decision.settled:
                counts["settled"] += 1
            elif decision.next_wave in counts:
                counts[decision.next_wave] += 1
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
    os.replace(part, out_jsonl)
    return counts
