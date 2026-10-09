from __future__ import annotations
from classifier.config import Thresholds
from classifier.gate import rule_scores


def _top2(scores: dict) -> tuple[str, float, str, float]:
    r = sorted(scores.items(), key=lambda x: -x[1])
    a, b = r[0], (r[1] if len(r) > 1 else (None, 0.0))
    return a[0], a[1], (b[0] or ""), b[1]


def classify_l1(query: str, labeled: dict[str, str] | list[str], emb, llm, th: Thresholds) -> dict:
    ids = list(labeled.keys()) if isinstance(labeled, dict) else list(labeled)
    descs = [labeled[i] for i in ids] if isinstance(labeled, dict) else list(labeled)
    try:
        vals = emb.score(query, descs)
    except Exception as e:
        return {"level1": None, "source": "MANUAL", "reason": f"embedding_error: {e}",
                "scores": {}, "margin": 0.0}
    scores = dict(zip(ids, [float(v) for v in vals]))
    rb = rule_scores(query, ids)
    scores = {k: v + rb.get(k, 0.0) for k, v in scores.items()}
    top, tv, second, sv = _top2(scores)
    margin = tv - sv
    if tv >= th.l1_direct_min and margin >= th.l1_direct_margin:
        return {"level1": top, "source": "BGE_DIRECT", "scores": scores, "margin": margin,
                "reason": "clear"}
    try:
        res = llm.choose(query, descs, allow_uncertain=True)
    except Exception as e:
        return {"level1": None, "source": "MANUAL", "reason": f"llm_error: {e}",
                "scores": scores, "margin": margin}
    pick = (res.get("choice") or "").strip()
    if pick == "UNCERTAIN" or not pick:
        return {"level1": None, "source": "MANUAL", "reason": "llm_uncertain",
                "scores": scores, "margin": margin}
    if pick not in ids:
        return {"level1": None, "source": "MANUAL", "reason": "llm_new_category_rejected",
                "scores": scores, "margin": margin}
    return {"level1": pick, "source": "LLM_JUDGE", "scores": scores, "margin": margin,
            "reason": "llm_constrained"}


def classify_l2(query: str, parent: str, children: list[dict], emb, reranker, llm,
                th: Thresholds, is_vision: bool = False) -> dict:
    ids = [c["id"] for c in children]
    descs = [f"{c['name']} {c['objects']} {c['actions']}" for c in children]
    try:
        rec = sorted(zip(ids, emb.score(query, descs)), key=lambda x: -x[1])[:5]
    except Exception as e:
        return {"level2": None, "source": "MANUAL", "reason": f"embedding_error: {e}",
                "scores": {}, "margin": 0.0}
    top5_ids = [i for i, _ in rec]
    top5_desc = [descs[ids.index(i)] for i in top5_ids]
    try:
        rr = reranker.rerank(query, top5_desc)
    except Exception as e:
        return {"level2": None, "source": "MANUAL", "reason": f"reranker_error: {e}",
                "scores": dict(rec), "margin": 0.0}
    rscores = dict(zip(top5_ids, [float(v) for v in rr]))
    top, tv, second, sv = _top2(rscores)
    margin = tv - sv
    need_margin = th.l2_direct_margin
    need_min = th.l2_direct_min + (th.vision_direct_min_boost if is_vision else 0.0)
    tbd = [c["id"] for c in children if c.get("status") in ("TBD",)]
    if top in tbd:
        need_margin += th.tbd_direct_margin_boost
    if tv >= need_min and margin >= need_margin:
        src = "RERANKER_DIRECT"
        if top not in tbd:
            return {"level2": top, "source": src, "scores": rscores, "margin": margin,
                    "reason": "clear", "top3": top5_ids[:3]}
        return {"level2": None, "source": "MANUAL", "reason": "tbd_conservative_review",
                "scores": rscores, "margin": margin, "top3": top5_ids[:3]}
    top3 = sorted(rscores.items(), key=lambda x: -x[1])[:3]
    opts = [i for i, _ in top3]
    try:
        res = llm.choose(query, opts, allow_uncertain=True)
    except Exception as e:
        return {"level2": None, "source": "MANUAL", "reason": f"llm_error: {e}",
                "scores": rscores, "margin": margin, "top3": opts}
    pick = (res.get("choice") or "").strip()
    if pick == "UNCERTAIN" or not pick:
        return {"level2": None, "source": "MANUAL", "reason": "llm_uncertain",
                "scores": rscores, "margin": margin, "top3": opts}
    if pick not in ids:
        return {"level2": None, "source": "MANUAL", "reason": "llm_new_category_rejected",
                "scores": rscores, "margin": margin, "top3": opts}
    if pick in tbd:
        return {"level2": None, "source": "MANUAL", "reason": "tbd_conservative_review",
                "scores": rscores, "margin": margin, "top3": opts}
    return {"level2": pick, "source": "LLM_JUDGE", "scores": rscores, "margin": margin,
            "reason": "llm_constrained", "top3": opts}
