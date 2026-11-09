"""波次 2：用向量模型在 4 个一级、27 个二级里选一对。

候选说明来自业务分类 Excel 的三级和数据示例（contracts/phase1/scenes-v3.json），三级只作判别线索。
这里不复制文件。阈值没校准时，调用方必须把结果留在待复核，judge 会做这道检查。
"""
from __future__ import annotations

from classifier.config import Thresholds
from classifier.gate import gate
from classifier.levels import classify_l1, classify_l2
from classifier.taxonomy_v3 import L1, L2, scene_descriptions


def make_scene_classifier(emb, reranker, llm, thresholds: Thresholds | None = None):
    """返回一个函数：文本进，候选结论出。emb 是 bge-m3，reranker 是 bge-reranker-v2-m3。"""
    th = thresholds or Thresholds(calibrated=False)
    knowledge = scene_descriptions()
    labeled = {key: knowledge["l1"][key] for key in L1}

    def classify(text: str) -> dict:
        query = text[: th.max_evidence_chars]
        relevance = gate(query, labeled, emb, th)
        base = {"calibrated": th.calibrated, "confirmed_unmatched": False,
                "level1": None, "level2": None, "scores": {"gate": relevance.get("scores", {})}}
        if relevance["status"] == "IRRELEVANT":
            return {**base, "confirmed_unmatched": True, "reason": "confirmed_unmatched"}
        if relevance["status"] != "RELEVANT":
            return {**base, "reason": relevance.get("reason") or "relevance_uncertain"}
        level1 = classify_l1(query, labeled, emb, llm, th)
        base["scores"]["l1"] = level1.get("scores", {})
        parent = level1.get("level1")
        if not parent:
            return {**base, "reason": level1.get("reason") or "level1_uncertain"}
        children = [
            {"id": name, "name": knowledge["l2"][parent][name], "objects": "", "actions": "",
             "status": "confirmed"}
            for name in L2[parent]
        ]
        level2 = classify_l2(query, parent, children, emb, reranker, llm, th)
        base["scores"]["l2"] = level2.get("scores", {})
        return {
            **base,
            "level1": parent,
            "level2": level2.get("level2"),
            "reason": level2.get("reason") or level1.get("reason") or "",
            "source": level2.get("source") or level1.get("source") or "",
        }

    return classify
