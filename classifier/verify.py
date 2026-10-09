from __future__ import annotations


def verify(query: str, l2: dict, siblings: list[dict]) -> dict:
    excl = (l2.get("exclude") or "")
    if not excl:
        return {"pass": True, "reason": "no_exclude_rule"}
    for sib in siblings:
        if sib["id"] == l2["id"]:
            continue
        for kw in [sib.get("objects", ""), sib.get("name", "")]:
            if kw and len(kw) >= 4 and kw[:4] in query and l2.get("objects", "")[:4] not in query:
                return {"pass": False, "reason": f"conflict_with_{sib['id']}",
                        "detail": f"evidence closer to {sib['id']}, chosen {l2['id']}"}
    return {"pass": True, "reason": "checked"}
