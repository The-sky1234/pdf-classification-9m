"""波次 2：用向量模型在 4 个一级、27 个二级里选一对。

这里不复制文件。阈值没校准时，调用方必须把结果留在待复核，judge 会做这道检查。
"""
from __future__ import annotations

from classifier.config import Thresholds
from classifier.gate import gate
from classifier.levels import classify_l1, classify_l2
from classifier.taxonomy_v3 import L1, L2


def make_scene_classifier(emb, reranker, llm, thresholds: Thresholds | None = None):
    """返回一个函数：文本进，候选结论出。emb 是 bge-m3，reranker 是 bge-reranker-v2-m3。"""
    th = thresholds or Thresholds(calibrated=False)

    def classify(text: str) -> dict:
        labeled = {key: name for key, name in L1.items()}
        relevance = gate(text, labeled, emb, th)
        if relevance["status"] == "IRRELEVANT":
            return {"calibrated": th.calibrated, "confirmed_unmatched": True,
                    "level1": None, "level2": None, "reason": "confirmed_unmatched"}
        if relevance["status"] != "RELEVANT":
            return {"calibrated": th.calibrated, "confirmed_unmatched": False,
                    "level1": None, "level2": None, "reason": relevance.get("reason") or "relevance_uncertain"}
        level1 = classify_l1(text, labeled, emb, llm, th)
        parent = level1.get("level1")
        if not parent:
            return {"calibrated": th.calibrated, "confirmed_unmatched": False,
                    "level1": None, "level2": None, "reason": level1.get("reason") or "level1_uncertain"}
        children = [
            {"id": name, "name": name, "objects": name, "actions": "", "status": "confirmed"}
            for name in L2[parent]
        ]
        level2 = classify_l2(text, parent, children, emb, reranker, llm, th)
        return {
            "calibrated": th.calibrated,
            "confirmed_unmatched": False,
            "level1": parent,
            "level2": level2.get("level2"),
            "reason": level2.get("reason") or level1.get("reason") or "",
        }

    return classify
