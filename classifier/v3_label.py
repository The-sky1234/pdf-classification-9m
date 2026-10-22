from __future__ import annotations
from classifier.config import Thresholds
from classifier.features import build
from classifier.taxonomy_v3 import L1, all_l2


def label_v3(parse: dict, emb, th: Thresholds | None = None) -> dict:
    """用 27 个场景名给一份解析结果打标签。

    正文里只出现一个场景名时采用该场景。否则用向量重叠，并沿用现有直接通过阈值。
    calibrated 为假时，调用方不得把结果写成业务归档。
    """
    th = th or Thresholds()
    feature = build(parse or {})
    query = (feature.get("query_text") or "").strip()
    if not query:
        return {"result_type": "MANUAL_REVIEW", "reason": "no_text"}
    hits = [(l1, l2) for l1, l2 in all_l2() if l2 and l2 in query]
    if len(hits) == 1:
        l1, l2 = hits[0]
        return _hit(l1, l2, th.calibrated, "scene_name")
    if len(hits) > 1:
        l1, l2 = hits[0]
        return {"result_type": "MANUAL_REVIEW", "level1": l1, "level2": l2,
                "reason": "multiple_scene_names", "calibrated": False}
    pairs = all_l2()
    try:
        scores = [float(v) for v in emb.score(query, [l2 for _l1, l2 in pairs])]
    except Exception as e:
        return {"result_type": "MANUAL_REVIEW", "reason": f"embedding_error: {e}"}
    ranked = sorted(zip(pairs, scores), key=lambda item: -item[1])
    (l1, l2), top = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    if top >= th.l2_direct_min and (top - second) >= th.l2_direct_margin:
        return _hit(l1, l2, th.calibrated, "embedding_clear")
    return {"result_type": "MANUAL_REVIEW", "level1": l1, "level2": l2,
            "reason": "low_margin", "calibrated": False}


def make_v3_classifier(th: Thresholds | None = None):
    from classifier.backends import KeywordEmbedding
    emb = KeywordEmbedding()
    thresholds = th or Thresholds()

    def classify(parse: dict) -> dict:
        return label_v3(parse, emb, thresholds)

    return classify


def _hit(l1: str, l2: str, calibrated: bool, reason: str) -> dict:
    return {"result_type": "CLASSIFIED", "level1": l1, "level2": l2,
            "calibrated": bool(calibrated), "reason": reason,
            "decision_source": reason, "level1_name": L1.get(l1, "")}
