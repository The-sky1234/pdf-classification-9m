"""读取已经去重好的文件清单。阶段一不再去重，清单里每个路径处理一次。

支持两种格式：
- 一行一个路径的文本；
- CSV，路径列叫「完整路径」等；有「是否主件」列时只取「是」；有「来源」「文件大小_字节」列时一并带上。
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


def parse_maps(specs: list[str]) -> list[tuple[str, str]]:
    """「旧前缀=新前缀」。清单是在别的挂载点上生成的时候，把路径换成服务器上看得到的样子。"""
    maps = []
    for spec in specs:
        old, sep, new = spec.partition("=")
        if not sep or not old:
            raise ValueError(f"--map 写成 旧前缀=新前缀：{spec}")
        maps.append((old, new))
    return sorted(maps, key=lambda m: len(m[0]), reverse=True)


def remap(path: str, maps: list[tuple[str, str]]) -> str:
    for old, new in maps:
        if path.startswith(old):
            tail = path[len(old):].replace("\\", "/")
            return new.rstrip("/") + "/" + tail.lstrip("/")
    return path


def under_roots(path: str, roots: list[str]) -> bool:
    return any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots)


def iter_mapped_rows(list_path: str, maps: list[tuple[str, str]]):
    for row in iter_master_rows(list_path):
        if maps:
            row["source_path"] = remap(row["source_path"], maps)
        yield row


def preflight(list_path: str, maps: list[tuple[str, str]], roots: list[str], sample: int) -> dict:
    """入库前整份过一遍：不在挂载根下的路径数、前 sample 条里打不开的路径数，各带几个例子。"""
    total = outside = checked = missing = 0
    outside_eg: list[str] = []
    missing_eg: list[str] = []
    for row in iter_mapped_rows(list_path, maps):
        total += 1
        path = row["source_path"]
        if roots and not under_roots(path, roots):
            outside += 1
            if len(outside_eg) < 5:
                outside_eg.append(path)
            continue
        if checked < sample:
            checked += 1
            if not os.path.isfile(path):
                missing += 1
                if len(missing_eg) < 5:
                    missing_eg.append(path)
    return {"total": total, "outside_roots": outside, "outside_examples": outside_eg,
            "checked": checked, "missing": missing, "missing_examples": missing_eg}


def missing_paths(paths) -> list[str]:
    return [path for path in paths if not os.path.isfile(path)]
