"""波次 0。不占 GPU。

能定论的写入 jsonl，并在给出归档根时复制文件。
还要 OCR 或还要分类的，只写入 jsonl，next_wave 标明下一步。
"""
from __future__ import annotations

import json
import os

from phase1.archive import copy_settled
from phase1.judge import judge_file
from phase1.masters import iter_master_paths


def run_cpu_wave(masters_path: str, out_jsonl: str, archive_root: str | None = None) -> dict:
    counts = {"settled": 0, "ocr": 0, "classify": 0, "retry": 0, "archived": 0, "missing": 0}
    os.makedirs(os.path.dirname(os.path.abspath(out_jsonl)) or ".", exist_ok=True)
    with open(out_jsonl, "w", encoding="utf-8") as out:
        for path in iter_master_paths(masters_path):
            if not os.path.isfile(path):
                counts["missing"] += 1
                out.write(json.dumps({"source_path": path, "reason": "missing"}, ensure_ascii=False) + "\n")
                continue
            decision = judge_file(path)
            record = decision.to_dict()
            if decision.settled and archive_root:
                copied = copy_settled(path, archive_root, decision)
                record["archive"] = copied
                counts["archived"] += 1
            if decision.settled:
                counts["settled"] += 1
            elif decision.next_wave in counts:
                counts[decision.next_wave] += 1
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
    return counts
