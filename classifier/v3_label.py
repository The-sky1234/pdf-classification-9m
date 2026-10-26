from __future__ import annotations
import os
from classifier.config import Thresholds
from classifier.features import build
from classifier.gate import rule_scores
from classifier.scene_kb import L1_TEXT, SCENE_TEXT
from classifier.taxonomy_v3 import L1, L2, all_l2


def label_v3(parse: dict, emb, th: Thresholds | None = None) -> dict:
    """用 27 个场景名给一份解析结果打标签。

    正文里只出现一个场景名时采用该场景。否则用向量重叠，并沿用现有直接通过阈值。
    calibrated 为假时，调用方不得把结果写成业务归档。
    """
    th = th or Thresholds()
    feature = build(parse or {})
    query = (feature.get("query_text") or "").strip()
    version = getattr(emb, "model_version", "")
    if not query:
        return {"result_type": "MANUAL_REVIEW", "reason": "no_text", "model_version": version}
    hits = [(l1, l2) for l1, l2 in all_l2() if l2 and l2 in query]
    if len(hits) == 1:
        l1, l2 = hits[0]
        return _hit(l1, l2, th.calibrated, "scene_name", model_version=version)
    if len(hits) > 1:
        l1, l2 = hits[0]
        return {"result_type": "MANUAL_REVIEW", "level1": l1, "level2": l2,
                "reason": "multiple_scene_names", "calibrated": False,
                "model_version": version}
    pairs = all_l2()
    try:
        scores = [float(v) for v in emb.score(query, [l2 for _l1, l2 in pairs])]
    except Exception as e:
        return {"result_type": "MANUAL_REVIEW", "reason": f"embedding_error: {e}",
                "model_version": version}
    ranked = sorted(zip(pairs, scores), key=lambda item: -item[1])
    (l1, l2), top = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    if top >= th.l2_direct_min and (top - second) >= th.l2_direct_margin:
        return _hit(l1, l2, th.calibrated, "embedding_clear", model_version=version)
    return {"result_type": "MANUAL_REVIEW", "level1": l1, "level2": l2,
            "reason": "low_margin", "calibrated": False, "model_version": version}


def make_v3_classifier(th: Thresholds | None = None):
    """字符重叠替身。只在 CLASSIFIER_BACKEND=keyword 时使用，不能当作生产模型。"""
    from classifier.backends import KeywordEmbedding
    emb = KeywordEmbedding()
    thresholds = th or Thresholds()

    def classify(parse: dict) -> dict:
        return label_v3(parse, emb, thresholds)

    classify.model_version = emb.model_version
    return classify


def make_model_classifier(th: Thresholds | None = None):
    """BGE-M3 召回，Reranker 精排，分数接近时才调用 LLM。缺模型立即失败。"""
    from classifier.backends import BgeM3Backend, CrossEncoderBackend, LLMHttpBackend
    emb = BgeM3Backend()
    reranker = CrossEncoderBackend()
    llm = LLMHttpBackend(os.environ.get("LLM_ENDPOINT", ""))
    emb.ensure_available()
    reranker.ensure_available()
    emb.score("分类预热", [L1_TEXT["L1_AQSC"]])
    reranker.rerank("分类预热", [SCENE_TEXT["消防监管"]])
    thresholds = th or Thresholds()

    def classify(parse: dict) -> dict:
        return judge_v3(parse, emb, reranker, llm, thresholds)

    classify.model_version = f"{emb.model_version}+{reranker.model_version}"
    return classify


def make_runtime_classifier(th: Thresholds | None = None):
    mode = os.environ.get("CLASSIFIER_BACKEND", "model").strip().lower()
    if mode in ("keyword", "mock"):
        return make_v3_classifier(th)
    if mode not in ("", "model", "bge"):
        raise RuntimeError(f"未知 CLASSIFIER_BACKEND={mode}")
    return make_model_classifier(th)


def classifier_plan() -> dict:
    mode = os.environ.get("CLASSIFIER_BACKEND", "model").strip().lower() or "model"
    return {
        "backend": mode,
        "embedding": os.environ.get("BGE_MODEL", "BAAI/bge-m3"),
        "reranker": os.environ.get("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3"),
        "llm": os.environ.get("LLM_MODEL", "Qwen2.5-7B-Instruct"),
        "llm_endpoint_configured": bool(os.environ.get("LLM_ENDPOINT", "").strip()),
        "calls_llm_for_every_file": False,
        "uses_mock": mode in ("keyword", "mock"),
    }


def judge_v3(parse: dict, emb, reranker, llm, th: Thresholds | None = None) -> dict:
    """按知识库说明做两级判断。

    一级先用向量和规则。一级明确后，只在该一级下面取向量前五，再交给 Reranker。
    Reranker 领先不足时，才把前三名交给 LLM。LLM 仍不能确定就进入复核。
    """
    th = th or Thresholds()
    query = (build(parse or {}).get("query_text") or "").strip()
    emb_name = getattr(emb, "model_version", "")
    rerank_name = getattr(reranker, "model_version", "")
    model_version = emb_name if not rerank_name else f"{emb_name}+{rerank_name}"
    base = {"model_version": model_version, "taxonomy_version": "v3.0-phase1", "calibrated": False}
    if not query:
        return {**base, "result_type": "MANUAL_REVIEW", "reason": "no_text"}
    l1_ids = list(L1)
    try:
        raw = [float(value) for value in emb.score(query, [L1_TEXT[item] for item in l1_ids])]
    except Exception as exc:
        return {**base, "result_type": "MANUAL_REVIEW", "reason": f"embedding_error: {exc}"}
    if len(raw) != len(l1_ids):
        return {**base, "result_type": "MANUAL_REVIEW", "reason": "embedding_error: score count"}
    rules = rule_scores(query, l1_ids)
    l1_scores = {item: raw[index] + rules.get(item, 0.0) for index, item in enumerate(l1_ids)}
    ranked = sorted(l1_scores.items(), key=lambda item: -item[1])
    top_id, top_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0
    margin = top_score - second_score
    scores = {"l1": l1_scores, "l1_margin": margin}
    if top_score < th.relevance_irrelevant and all(value < 0.35 for value in l1_scores.values()):
        return {**base, "result_type": "IRRELEVANT", "reason": "all_low",
                "calibrated": bool(th.calibrated), "confirmed_unmatched": True,
                "decision_source": "BGE_DIRECT", "candidate_scores": scores}
    l1_source = "BGE_DIRECT"
    if not (top_score >= th.l1_direct_min and margin >= th.l1_direct_margin):
        picked = _ask(llm, _l1_evidence(query, ranked[:2]), [item for item, _score in ranked[:2]])
        if picked is None:
            return {**base, "result_type": "MANUAL_REVIEW", "level1": top_id,
                    "reason": "l1_uncertain", "decision_source": "MANUAL",
                    "candidate_scores": scores, "prompt_version": _prompt_version(llm)}
        top_id = picked
        l1_source = "LLM_JUDGE"
    children = L2[top_id]
    try:
        child_scores = [float(value) for value in emb.score(
            query, [SCENE_TEXT[name] for name in children])]
    except Exception as exc:
        return {**base, "result_type": "MANUAL_REVIEW", "level1": top_id,
                "reason": f"embedding_error: {exc}", "decision_source": "MANUAL",
                "candidate_scores": scores}
    if len(child_scores) != len(children):
        return {**base, "result_type": "MANUAL_REVIEW", "level1": top_id,
                "reason": "embedding_error: score count", "candidate_scores": scores}
    top5 = [name for name, _score in sorted(zip(children, child_scores), key=lambda item: -item[1])[:5]]
    try:
        reranked = [float(value) for value in reranker.rerank(
            query, [SCENE_TEXT[name] for name in top5])]
    except Exception as exc:
        return {**base, "result_type": "MANUAL_REVIEW", "level1": top_id, "level2": top5[0],
                "reason": f"reranker_error: {exc}", "decision_source": "MANUAL",
                "candidate_scores": scores}
    if len(reranked) != len(top5):
        return {**base, "result_type": "MANUAL_REVIEW", "level1": top_id, "level2": top5[0],
                "reason": "reranker_error: score count", "candidate_scores": scores}
    order = sorted(zip(top5, reranked), key=lambda item: -item[1])
    best, best_score = order[0]
    runner = order[1][1] if len(order) > 1 else 0.0
    l2_margin = best_score - runner
    scores["l2"] = dict(zip(top5, reranked))
    scores["l2_margin"] = l2_margin
    scores["l1_source"] = l1_source
    if best_score >= th.l2_direct_min and l2_margin >= th.l2_direct_margin:
        return _hit(top_id, best, th.calibrated, "clear", source="RERANKER_DIRECT",
                    model_version=model_version, candidate_scores=scores)
    top3 = [name for name, _score in order[:3]]
    picked = _ask(llm, _l2_evidence(query, top3), top3)
    if picked in top3:
        return _hit(top_id, picked, th.calibrated, "llm_constrained", source="LLM_JUDGE",
                    model_version=model_version, candidate_scores=scores,
                    prompt_version=_prompt_version(llm))
    return {**base, "result_type": "MANUAL_REVIEW", "level1": top_id, "level2": best,
            "reason": "llm_uncertain", "decision_source": "MANUAL",
            "candidate_scores": scores, "prompt_version": _prompt_version(llm)}


def _ask(llm, evidence: str, options: list[str]) -> str | None:
    if llm is None or not options:
        return None
    try:
        result = llm.choose(evidence, options, allow_uncertain=True)
    except Exception:
        return None
    choice = str((result or {}).get("choice") or "").strip()
    if choice in options:
        return choice
    return None


def _l1_evidence(query: str, ranked: list[tuple[str, float]]) -> str:
    lines = [query[:4000], "候选一级："]
    for item, _score in ranked:
        lines.append(f"{item} {L1.get(item, '')}：{L1_TEXT[item]}")
    return "\n".join(lines)


def _l2_evidence(query: str, names: list[str]) -> str:
    lines = [query[:4000], "候选二级："]
    for name in names:
        lines.append(SCENE_TEXT[name])
    return "\n".join(lines)


def _prompt_version(llm) -> str:
    return getattr(llm, "prompt_version", "") or ""


def _hit(l1: str, l2: str, calibrated: bool, reason: str, source: str | None = None,
         model_version: str = "", candidate_scores: dict | None = None,
         prompt_version: str = "") -> dict:
    out = {"result_type": "CLASSIFIED", "level1": l1, "level2": l2,
           "calibrated": bool(calibrated), "reason": reason,
           "decision_source": source or reason, "level1_name": L1.get(l1, ""),
           "model_version": model_version, "taxonomy_version": "v3.0-phase1"}
    if candidate_scores is not None:
        out["candidate_scores"] = candidate_scores
    if prompt_version:
        out["prompt_version"] = prompt_version
    return out
