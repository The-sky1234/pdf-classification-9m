"""读取上游去重留下的主件清单。一行一个路径，或 CSV 里的「完整路径」列。"""
from __future__ import annotations

import csv
import os

_PATH_COLUMNS = ("完整路径", "路径", "path", "full_path", "source_path")


def iter_master_paths(list_path: str):
    """按清单原顺序产出路径。空行和以 # 开头的行跳过。不访问这些路径上的文件。"""
    if list_path.lower().endswith(".csv"):
        yield from _from_csv(list_path)
        return
    with open(list_path, encoding="utf-8-sig") as handle:
        for line in handle:
            path = line.strip().strip('"')
            if path and not path.startswith("#"):
                yield path


def _from_csv(list_path: str) -> None:
    with open(list_path, encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            return
        column = next((name for name in reader.fieldnames if name in _PATH_COLUMNS), None)
        if column is None:
            raise ValueError("主件 CSV 里没有路径列")
        for row in reader:
            path = (row.get(column) or "").strip()
            if path:
                yield path


def missing_paths(paths) -> list[str]:
    return [path for path in paths if not os.path.isfile(path)]
