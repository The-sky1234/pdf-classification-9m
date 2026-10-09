from __future__ import annotations
import re


def _parse_simple_yaml_taxonomy(text: str) -> dict:
    """最小解析：仅提取level1 id/name/definition/confusion + children行内对象。"""
    l1s: list[dict] = []
    cur = None
    for raw in text.splitlines():
        line = raw.strip()
        m = re.match(r"-\s*id:\s*(\S+)", line)
        if m and line.startswith("- id:"):
            cur = {"id": m.group(1), "name": "", "definition": "", "confusion": "",
                   "status": "", "children": []}
            l1s.append(cur)
            continue
        if cur is not None:
            if line.startswith("name:") and "children" not in raw:
                cur["name"] = line.split(":", 1)[1].strip()
            elif line.startswith("definition:"):
                cur["definition"] = line.split(":", 1)[1].strip()
            elif line.startswith("confusion_note:"):
                cur["confusion"] = line.split(":", 1)[1].strip().strip('"')
            elif line.startswith("status:"):
                cur["status"] = line.split(":", 1)[1].strip()
            elif line.startswith("- {id:"):
                body = line[3:].strip("{} ")
                kv: dict[str, str] = {}
                for part in re.split(r",\s*(?=\w+:)", body):
                    if ":" in part:
                        k, v = part.split(":", 1)
                        kv[k.strip()] = v.strip()
                cur["children"].append({"id": kv.get("id", ""), "name": kv.get("name", ""),
                                        "objects": kv.get("objects", ""), "actions": kv.get("actions", ""),
                                        "exclude": kv.get("exclude", ""), "status": kv.get("status", "")})
    return {"level1": l1s}


def load_taxonomy(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        text = f.read()
    tb = _parse_simple_yaml_taxonomy(text)
    l1 = tb["level1"]
    assert len(l1) == 4, f"taxonomy must have 4 L1, got {len(l1)}"
    total_l2 = sum(len(x["children"]) for x in l1)
    assert total_l2 == 25, f"taxonomy must have 25 L2, got {total_l2}"
    l2_by_l1 = {x["id"]: [c["id"] for c in x["children"]] for x in l1}
    l2_info = {c["id"]: {**c, "parent": x["id"]} for x in l1 for c in x["children"]}
    l1_info = {x["id"]: x for x in l1}
    return {"level1": l1_info, "level2": l2_info, "l2_by_l1": l2_by_l1,
            "taxonomy_version": "v1.0-planner"}
