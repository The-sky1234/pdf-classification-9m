"""读取上游去重留下的主件清单。

支持两种格式：
- 一行一个路径的文本；
- CSV，路径列叫「完整路径」等；有「是否主件」列时只取「是」；有「来源」「文件大小_字节」列时一并带上。
  tools/dedup/exact_dedup_from_listing.py masters 输出的 masters.csv 就是这种。
编码按 UTF-8（可带 BOM）读，整份解不开再按 GB18030。
"""
from __future__ import annotations

import csv
import os

_PATH_COLUMNS = ("完整路径", "路径", "path", "full_path", "source_path")
_SIZE_COLUMNS = ("文件大小_字节", "文件大小", "size", "bytes")
_SOURCE_COLUMNS = ("来源", "source_from")
_MASTER_COLUMNS = ("是否主件", "is_master")

csv.field_size_limit(2**31 - 1)


def _encoding(list_path: str) -> str:
    try:
        with open(list_path, encoding="utf-8-sig") as probe:
            while probe.read(8 * 1024 * 1024):
                pass
        return "utf-8-sig"
    except UnicodeDecodeError:
        return "gb18030"


def iter_master_rows(list_path: str):
    """按清单原顺序产出 {source_path, size_bytes, source_from}。不访问这些路径上的文件。"""
    encoding = _encoding(list_path)
    if list_path.lower().endswith(".csv"):
        yield from _from_csv(list_path, encoding)
        return
    with open(list_path, encoding=encoding) as handle:
        for line in handle:
            path = line.strip().strip('"')
            if path and not path.startswith("#"):
                yield {"source_path": path, "size_bytes": None, "source_from": None}


def iter_master_paths(list_path: str):
    for row in iter_master_rows(list_path):
        yield row["source_path"]


def _pick(fieldnames, names):
    stripped = {(name or "").strip(): name for name in fieldnames}
    return next((stripped[n] for n in names if n in stripped), None)


def _from_csv(list_path: str, encoding: str):
    with open(list_path, encoding=encoding, newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            return
        column = _pick(reader.fieldnames, _PATH_COLUMNS)
        if column is None:
            raise ValueError("主件 CSV 里没有路径列")
        size_col = _pick(reader.fieldnames, _SIZE_COLUMNS)
        source_col = _pick(reader.fieldnames, _SOURCE_COLUMNS)
        master_col = _pick(reader.fieldnames, _MASTER_COLUMNS)
        for row in reader:
            path = (row.get(column) or "").strip()
            if not path:
                continue
            if master_col and (row.get(master_col) or "").strip() not in ("是", "1", "true", "True", "yes"):
                continue
            size = (row.get(size_col) or "").strip().replace(",", "") if size_col else ""
            yield {
                "source_path": path,
                "size_bytes": int(size) if size.isdigit() else None,
                "source_from": ((row.get(source_col) or "").strip() or None) if source_col else None,
            }


def missing_paths(paths) -> list[str]:
    return [path for path in paths if not os.path.isfile(path)]
