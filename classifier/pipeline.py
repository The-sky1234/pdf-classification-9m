from __future__ import annotations
from classifier.config import Thresholds
from classifier.knowledge import load_taxonomy
from classifier.evidence import route_evidence
from classifier.gate import gate
from classifier.levels import classify_l1, classify_l2
from classifier.verify import verify
from classifier.review import AutoReviewer

TAXONOMY_PATH = "contracts/taxonomy.yaml"


class ClassificationPipeline:
    def __init__(self, emb, reranker, llm, th: Thresholds | None = None,
                 taxonomy_path: str = TAXONOMY_PATH):
        self.emb = emb
        self.reranker = reranker
        self.llm = llm
        self.th = th or Thresholds()
        self.kb = load_taxonomy(taxonomy_path)
        self.reviewer = AutoReviewer(llm, self.kb)

    def _auto(self, result: dict) -> dict:
        """ISSUE-07门禁：未校准阈值不得自动归档，降为MANUAL_REVIEW。"""
        if self.th.calibrated:
            return result
        return {"file_path": result["file_path"], "result_type": "MANUAL_REVIEW",
                "relevance_status": result.get("relevance_status"),
                "evidence_mode": result.get("evidence_mode"),
                "level1": None, "level2": None,
                "candidate_scores": result.get("candidate_scores"),
                "decision_source": "MANUAL",
                "classification_basis": result.get("classification_basis"),
                "model_version": result.get("model_version"),
                "taxonomy_version": result.get("taxonomy_version"),
                "reason": "uncalibrated_thresholds_blocked"}

    def _l1_cands(self) -> list[dict]:
        return [{"id": lid, "name": self.kb["level1"][lid]["name"],
                 "objects": self.kb["level1"][lid]["definition"], "actions": "",
                 "exclude": self.kb["level1"][lid].get("confusion", ""),
                 "parent": None, "status": "confirmed"}
                for lid in self.kb["level1"]]

    def _supplement(self, ev: dict) -> str:
        heads = [h.get("text", "") if isinstance(h, dict) else str(h)
                 for h in ev["bundle"].get("headings", [])]
        extra = [p["text"] for p in ev["bundle"].get("paras", [])[3:6]]
        return "\n".join(heads + extra)[:2000]

    def _review_l1(self, file_path, ev, q, basis, g, reason_in, relevance,
                   ocr_score=None):
        out = self.reviewer.review(
            file_path, ev["mode"], ev["bundle"], q, self._l1_cands(), "L1", None,
            relevance, reason_in, {"gate": g["scores"]}, self.emb.model_version,
            ocr_score=ocr_score, supplement=self._supplement(ev))
        if out["result_type"] == "NEED_L2":
            return self._run_l2(file_path, ev, q, basis, g, out["level1"],
                            ocr_score=ocr_score)
        if out["result_type"] in ("CLASSIFIED", "IRRELEVANT"):
            return self._auto(out)
        return out

    def _run_l2(self, file_path, ev, q, basis, g, l1_id, ocr_score=None):
        children = [dict(c, parent=l1_id) for c in self.kb["level1"][l1_id]["children"]]
        l2 = classify_l2(q, l1_id, children, self.emb, self.reranker, self.llm,
                         self.th, is_vision=(ev["mode"] == "VISION"))
        if not l2["level2"]:
            top3 = l2.get("top3") or [c["id"] for c in children[:3]]
            cands = [c for c in children if c["id"] in top3] or children
            out = self.reviewer.review(
                file_path, ev["mode"], ev["bundle"], q, cands, "L2", l1_id,
                "RELEVANT", l2["reason"],
                {"gate": g["scores"], "l2": l2["scores"], "l2_margin": l2["margin"]},
                self.emb.model_version, ocr_score=ocr_score,
                supplement=self._supplement(ev))
            if out["result_type"] == "CLASSIFIED":
                return self._verify_and_close(file_path, ev, q, basis, out, children)
            return out
        return self._verify_and_close(
            file_path, ev, q, basis,
            {"file_path": file_path, "result_type": "CLASSIFIED",
             "relevance_status": "RELEVANT", "evidence_mode": ev["mode"],
             "level1": l1_id, "level2": l2["level2"],
             "candidate_scores": {"gate": g["scores"], "l2": l2["scores"],
                                  "l2_margin": l2["margin"]},
             "decision_source": l2["source"], "classification_basis": basis,
             "model_version": self.emb.model_version,
             "taxonomy_version": self.kb["taxonomy_version"],
             "reason": f"l2={l2['source']}",
             "review_stats": self.reviewer.stats.to_dict()},
            children)

    def _verify_and_close(self, file_path, ev, q, basis, out, children):
        l2info = next(c for c in children if c["id"] == out["level2"])
        if verify(q, l2info, children)["pass"]:
            return self._auto(out)
        retry = self.reviewer.review(
            file_path, ev["mode"], ev["bundle"], q, children, "L2", out["level1"],
            "RELEVANT", "verify_conflict",
            {**(out.get("candidate_scores") or {})},
            self.emb.model_version, supplement=self._supplement(ev))
        if retry["result_type"] == "CLASSIFIED":
            l2b = next(c for c in children if c["id"] == retry["level2"])
            if verify(q, l2b, children)["pass"]:
                return self._auto(retry)
        if retry["result_type"] == "FAILED":
            return retry
        retry["result_type"] = "UNRESOLVED"
        retry["level1"] = out["level1"]
        retry["level2"] = None
        retry["reason"] = "verify_conflict"
        return retry

    def run(self, parse: dict) -> dict:
        file_path = parse.get("file_path", "")
        ev = route_evidence(parse, self.th)
        q = ev["query_text"]
        l1_ids = list(self.kb["level1"].keys())
        l1_desc = {lid: f"{self.kb['level1'][lid]['name']} {self.kb['level1'][lid]['definition']}"
                   for lid in l1_ids}
        basis = {"mode": ev["mode"], "title": ev["bundle"]["title"],
                 "paras": ev["bundle"]["paras"][:3],
                 "visual": bool(ev["bundle"]["visual"])}
        scores = [pg.get("ocr_score") for pg in (parse.get("pages") or [])
                  if pg.get("ocr_score") is not None]
        ocr_mean = sum(scores) / len(scores) if scores else None
        g = gate(q, l1_desc, self.emb, self.th)
        if g["status"] == "IRRELEVANT":
            return self._auto({"file_path": file_path, "result_type": "IRRELEVANT",
                               "relevance_status": "IRRELEVANT", "evidence_mode": ev["mode"],
                               "level1": None, "level2": None,
                               "candidate_scores": {"gate": g["scores"]},
                               "decision_source": "BGE_DIRECT",
                               "classification_basis": basis,
                               "model_version": self.emb.model_version,
                               "taxonomy_version": self.kb["taxonomy_version"],
                               "reason": g["reason"]})
        if g["status"] == "UNCERTAIN":
            if g["reason"].startswith("embedding_error"):
                return {"file_path": file_path, "result_type": "FAILED",
                        "relevance_status": "UNCERTAIN", "evidence_mode": ev["mode"],
                        "level1": None, "level2": None,
                        "candidate_scores": {"gate": g["scores"]},
                        "decision_source": "MANUAL", "classification_basis": basis,
                        "model_version": self.emb.model_version,
                        "taxonomy_version": self.kb["taxonomy_version"],
                        "reason": g["reason"],
                        "error": {"code": "EMBEDDING_FAILED", "message": g["reason"],
                                  "retryable": True}}
            return self._review_l1(file_path, ev, q, basis, g, g["reason"], "UNCERTAIN",
                               ocr_score=ocr_mean)
        l1 = classify_l1(q, l1_desc, self.emb, self.llm, self.th)
        if not l1["level1"]:
            return self._review_l1(file_path, ev, q, basis, g, l1["reason"], "RELEVANT",
                               ocr_score=ocr_mean)
        return self._run_l2(file_path, ev, q, basis, g, l1["level1"],
                            ocr_score=ocr_mean)

    def _review(self, fp, ev, rel, l1, l2, cand, src, basis, reason):
        return {"file_path": fp, "result_type": "MANUAL_REVIEW",
                "relevance_status": rel, "evidence_mode": ev["mode"],
                "level1": l1, "level2": l2, "candidate_scores": cand,
                "decision_source": "MANUAL", "classification_basis": basis,
                "model_version": self.emb.model_version,
                "taxonomy_version": self.kb["taxonomy_version"], "reason": reason}


def classify(parse: dict, emb, reranker, llm, th: Thresholds | None = None) -> dict:
    return ClassificationPipeline(emb, reranker, llm, th).run(parse)
