from __future__ import annotations
from classifier.config import Thresholds

DOMAIN_HITS: dict[str, list[str]] = {
    "L1_AQSC": ["安全", "监管", "执法", "检查", "隐患", "事故调查", "危险化学", "矿山", "消防", "safety"],
    "L1_YJJY": ["预案", "救援", "应急", "指挥", "调度", "演练", "rescue"],
    "L1_ZHGL": ["党建", "人事", "财务", "预算", "宣传", "信息化", "办公", "政策法规"],
    "L1_FZZJ": ["防汛", "抗旱", "地震", "地质灾害", "森林", "草原", "救灾", "物资保障", "减灾", "监测预警"],
}


def rule_scores(query: str, l1_ids: list[str]) -> dict[str, float]:
    scores = {}
    for lid in l1_ids:
        hits = sum(1 for kw in DOMAIN_HITS.get(lid, []) if kw and kw in query)
        scores[lid] = min(0.30, 0.10 * hits)
    return scores


def gate(query: str, labeled: dict[str, str], emb, th: Thresholds) -> dict:
    ids = list(labeled.keys())
    descs = [labeled[i] for i in ids]
    try:
        emb_scores = emb.score(query, descs)
    except Exception as e:
        return {"status": "UNCERTAIN", "reason": f"embedding_error: {e}",
                "scores": {}, "margin": 0.0}
    if not query.strip():
        return {"status": "UNCERTAIN", "reason": "empty_evidence",
                "scores": dict(zip(descs, emb_scores)), "margin": 0.0}
    rules = rule_scores(query, ids)
    combined = {lid: float(s) + rules.get(lid, 0.0) for lid, s in zip(ids, emb_scores)}
    ranked = sorted(combined.items(), key=lambda x: -x[1])
    top, second = ranked[0][1], (ranked[1][1] if len(ranked) > 1 else 0.0)
    if top < th.relevance_irrelevant and all(v < 0.35 for v in combined.values()):
        return {"status": "IRRELEVANT", "reason": "all_low", "scores": combined,
                "margin": top - second}
    if top >= th.relevance_relevant:
        return {"status": "RELEVANT", "reason": "strong_match", "scores": combined,
                "margin": top - second}
    return {"status": "UNCERTAIN", "reason": "weak_or_ambiguous", "scores": combined,
            "margin": top - second}
