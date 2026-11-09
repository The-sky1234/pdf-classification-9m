"""把《数据集业务分类信息.xlsx》转成波次 2 用的场景说明。

    python tools/build_scene_knowledge.py docs/deliverables/数据集业务分类信息.xlsx contracts/phase1/scenes-v3.json

一级、二级名称必须和 classifier/taxonomy_v3.py 一字不差，否则直接报错。
三级只作为判别线索写进说明，不建目录。需要 openpyxl，只在开发机上跑一次。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from classifier.taxonomy_v3 import L1, L2  # noqa: E402


def read_rows(xlsx: str) -> list[tuple[str, str, str, str]]:
    import openpyxl

    sheet = openpyxl.load_workbook(xlsx, read_only=True).worksheets[0]
    rows = []
    l1 = l2 = ""
    for i, row in enumerate(sheet.iter_rows(values_only=True)):
        if i == 0:
            continue
        _, c1, c2, c3, sample = (list(row) + [None] * 5)[:5]
        l1 = (c1 or l1 or "").strip()
        l2 = (c2 or l2 or "").strip()
        if not (c3 or "").strip():
            continue
        rows.append((l1, l2, str(c3).strip(), str(sample or "").strip().rstrip("。")))
    return rows


def build(rows) -> dict:
    name_to_id = {name: key for key, name in L1.items()}
    scenes: dict[str, dict[str, list[dict]]] = {key: {} for key in L1}
    for l1, l2, l3, sample in rows:
        if l1 not in name_to_id:
            raise SystemExit(f"Excel 一级「{l1}」不在 taxonomy_v3")
        key = name_to_id[l1]
        if l2 not in L2[key]:
            raise SystemExit(f"Excel 二级「{l1}/{l2}」不在 taxonomy_v3")
        scenes[key].setdefault(l2, []).append({"l3": l3, "sample": sample})
    for key, names in L2.items():
        missing = [name for name in names if name not in scenes[key]]
        if missing:
            raise SystemExit(f"taxonomy_v3 的二级在 Excel 里没有三级线索：{L1[key]}/{missing}")
    out = {"version": "v3", "l1": {}, "l2": {}}
    for key, names in L2.items():
        out["l1"][key] = f"{L1[key]}：" + "、".join(names)
        out["l2"][key] = {}
        for name in names:
            clues = "；".join(
                f"{item['l3']}（{item['sample']}）" if item["sample"] else item["l3"]
                for item in scenes[key][name]
            )
            out["l2"][key][name] = f"{L1[key]}-{name}：{clues}"
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    data = build(read_rows(argv[1]))
    with open(argv[2], "w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=1)
        handle.write("\n")
    print(f"wrote {argv[2]}: {sum(len(v) for v in data['l2'].values())} 个二级")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
