"""LLM自动复核（v1.1）：低置信/冲突/UNCERTAIN优先走LLM，最多2轮，第二轮先补证据。
只允许输出候选ID/IRRELEVANT/UNCERTAIN；2轮不定→UNRESOLVED；调用失败→FAILED。"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
import time

ALLOWED_EXTRA = ("IRRELEVANT", "UNCERTAIN")
CALL_FAILED = "LLM_CALL_FAILED"


@dataclass
class ReviewStats:
    calls: int = 0
    timeouts: int = 0
    call_failures: int = 0
    retries: int = 0
    cost_units: int = 0

    def to_dict(self) -> dict:
        return {"calls": self.calls, "timeouts": self.timeouts,
                "call_failures": self.call_failures, "retries": self.retries,
                "cost_units": self.cost_units}


class ReviewCallFailed(Exception):
    pass


def build_review_prompt(query: str, bundle: dict, candidates: list[dict],
                        taxonomy_version: str, parent: str | None = None) -> dict:
    return {
        "title": bundle.get("title", ""),
        "paras": bundle.get("paras", [])[:6],
        "visual": bundle.get("visual", ""),
        "candidates": [{"id": c["id"], "name": c.get("name", ""),
                        "objects": c.get("objects", ""), "actions": c.get("actions", ""),
                        "exclude": c.get("exclude", ""), "parent": c.get("parent", parent or ""),
                        "status": c.get("status", "")} for c in candidates],
        "constraints": {
            "taxonomy_version": taxonomy_version,
            "parent": parent,
            "parent_child": ("仅能在该一级的子类别中选择" if parent
                             else "一级类别四选一"),
            "allowed_outputs": [c["id"] for c in candidates] + ["IRRELEVANT", "UNCERTAIN"],
            "forbidden": "禁止创造新类别；证据不足选UNCERTAIN",
        },
    }


def _prompt_text(prompt: dict) -> str:
    lines = [f"标题：{prompt['title']}"]
    for p in prompt["paras"]:
        lines.append(f"[p{p.get('page_no', '?')}] {p.get('text', '')}")
    if prompt["visual"]:
        lines.append(f"视觉：{prompt['visual']}")
    for c in prompt["candidates"]:
        lines.append(f"候选 {c['id']}：{c['name']}｜对象：{c['objects']}｜动作：{c['actions']}｜"
                     f"排除：{c['exclude']}｜父类：{c['parent']}")
    lines.append(f"约束：{prompt['constraints']}")
    return "\n".join(lines)


class AutoReviewer:
    def __init__(self, llm, kb: dict, max_rounds: int = 2, timeout_s: float = 30.0,
                 max_retries: int = 2, min_ocr: float = 0.5):
        self.llm = llm
        self.kb = kb
        self.max_rounds = max_rounds
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.min_ocr = min_ocr
        self.stats = ReviewStats()

    @property
    def prompt_version(self) -> str:
        return getattr(self.llm, "prompt_version", "llm-judge-v1")

    def _call(self, text: str, options: list[str]) -> dict:
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self.stats.calls += 1
            self.stats.cost_units += 1
            try:
                with ThreadPoolExecutor(max_workers=1) as ex:
                    fut = ex.submit(self.llm.choose, text, options, True)
                    return fut.result(timeout=self.timeout_s)
            except FuturesTimeout as e:
                last = e
                self.stats.timeouts += 1
            except Exception as e:
                last = e
                self.stats.call_failures += 1
            self.stats.retries += 1
        raise ReviewCallFailed(f"{CALL_FAILED} after {self.max_retries} retries: {last}")

    def review(self, file_path: str, mode: str, bundle: dict, query: str,
               candidates: list[dict], level: str, parent: str | None,
               relevance: str, reason_in: str, scores: dict,
               model_version: str, ocr_score: float | None = None,
               supplement: str = "") -> dict:
        basis = {"mode": mode, "title": bundle.get("title", ""),
                 "paras": bundle.get("paras", [])[:3],
                 "visual": bool(bundle.get("visual", ""))}
        ids = [c["id"] for c in candidates]
        if mode == "OCR_BODY" and ocr_score is not None and ocr_score < self.min_ocr:
            return {"file_path": file_path, "result_type": "UNRESOLVED",
                    "relevance_status": relevance, "evidence_mode": mode,
                    "level1": parent, "level2": None,
                    "candidate_scores": scores, "decision_source": "MANUAL",
                    "classification_basis": basis, "model_version": model_version,
                    "taxonomy_version": self.kb["taxonomy_version"],
                    "reason": "ocr_insufficient_needs_reparse",
                    "suggested_action": "REPARSE_OR_VISION",
                    "review_stats": self.stats.to_dict()}
        text = _prompt_text(build_review_prompt(query, bundle, candidates,
                                                self.kb["taxonomy_version"], parent))
        options = ids + ["IRRELEVANT"]
        pick, note = "UNCERTAIN", reason_in
        for rnd in range(self.max_rounds):
            cur = text if rnd == 0 else text + "\n补充证据：\n" + supplement
            try:
                res = self._call(cur, options)
            except ReviewCallFailed as e:
                return {"file_path": file_path, "result_type": "FAILED",
                        "relevance_status": relevance, "evidence_mode": mode,
                        "level1": None, "level2": None, "candidate_scores": scores,
                        "decision_source": "MANUAL", "classification_basis": basis,
                        "model_version": model_version,
                        "taxonomy_version": self.kb["taxonomy_version"],
                        "reason": reason_in, "error": {"code": CALL_FAILED,
                                                      "message": str(e), "retryable": True},
                        "review_stats": self.stats.to_dict()}
            pick = (res.get("choice") or "UNCERTAIN").strip()
            if pick in ids or pick == "IRRELEVANT":
                break
            note = "llm_new_category_rejected" if pick not in ("UNCERTAIN", "") else res.get("reason", reason_in)
            pick = "UNCERTAIN"
        if pick == "IRRELEVANT":
            return {"file_path": file_path, "result_type": "IRRELEVANT",
                    "relevance_status": "IRRELEVANT", "evidence_mode": mode,
                    "level1": None, "level2": None, "candidate_scores": scores,
                    "decision_source": "LLM_JUDGE", "classification_basis": basis,
                    "model_version": model_version,
                    "taxonomy_version": self.kb["taxonomy_version"],
                    "reason": "review_irrelevant",
                    "review_stats": self.stats.to_dict()}
        if pick in ids:
            l1 = parent if level == "L2" else pick
            l2 = pick if level == "L2" else None
            if level == "L2" and pick not in self.kb["l2_by_l1"].get(parent or "", []):
                pick = "UNCERTAIN"
            else:
                return {"file_path": file_path, "result_type": "CLASSIFIED"
                        if level == "L2" else "NEED_L2",
                        "relevance_status": "RELEVANT", "evidence_mode": mode,
                        "level1": l1, "level2": l2, "candidate_scores": scores,
                        "decision_source": "LLM_JUDGE", "classification_basis": basis,
                        "model_version": model_version,
                        "taxonomy_version": self.kb["taxonomy_version"],
                        "reason": "review_resolved",
                        "review_stats": self.stats.to_dict()}
        return {"file_path": file_path, "result_type": "UNRESOLVED",
                "relevance_status": relevance, "evidence_mode": mode,
                "level1": parent if level == "L2" else None, "level2": None,
                "candidate_scores": scores, "decision_source": "MANUAL",
                "classification_basis": basis, "model_version": model_version,
                "taxonomy_version": self.kb["taxonomy_version"],
                "reason": note or "review_inconclusive",
                "candidates_kept": ids,
                "review_stats": self.stats.to_dict()}
