"""LLM自动复核单测+集成（v1.1）：裁决、补证据、冲突、OCR、失败分离、统计。"""
import time
import pytest
from classifier.review import AutoReviewer, build_review_prompt, ReviewCallFailed
from classifier.knowledge import load_taxonomy
from classifier.config import Thresholds
from classifier.pipeline import ClassificationPipeline
from classifier.backends import KeywordEmbedding, KeywordReranker, ScriptedLLM

KB = load_taxonomy("contracts/taxonomy.yaml")
CANDS = [{"id": "L2_WHX", "name": "危险化学品监管", "objects": "危化品企业",
          "actions": "检查", "exclude": "事故救援另判", "parent": "L1_AQSC",
          "status": "pending_review"},
         {"id": "L2_ZFGW", "name": "安全生产执法和工贸监管", "objects": "工贸企业",
          "actions": "执法检查", "exclude": "专业事项按职责", "parent": "L1_AQSC",
          "status": "TBD"}]
BUNDLE = {"title": "危化企业专项执法检查通知",
          "paras": [{"text": "危化企业执法检查。", "page_no": 1}], "visual": ""}


def rev(llm, **kw):
    return AutoReviewer(llm, KB, **kw)


def test_prompt_has_all_required_inputs():
    p = build_review_prompt("q", BUNDLE, CANDS, "v1.0-planner", parent="L1_AQSC")
    assert p["title"] and p["paras"] and len(p["candidates"]) == 2
    c0 = p["candidates"][0]
    assert {"id", "name", "objects", "actions", "exclude", "parent"} <= set(c0)
    cons = p["constraints"]
    assert cons["parent"] == "L1_AQSC" and "parent_child" in cons
    assert set(cons["allowed_outputs"]) == {"L2_WHX", "L2_ZFGW", "IRRELEVANT", "UNCERTAIN"}


def test_round1_success_and_stats():
    r = rev(ScriptedLLM(default_choice="L2_WHX"))
    out = r.review("f", "TITLE", BUNDLE, "q", CANDS, "L2", "L1_AQSC",
                   "RELEVANT", "r", {}, "m")
    assert out["result_type"] == "CLASSIFIED" and out["level2"] == "L2_WHX"
    assert out["review_stats"]["calls"] == 1


def test_round2_uses_supplemented_evidence():
    seen = []

    class RecLLM:
        prompt_version = "t"
        def choose(self, ev, opts, allow_uncertain=True):
            seen.append(ev)
            return {"choice": "UNCERTAIN" if len(seen) == 1 else "L2_WHX"}
    r = rev(RecLLM())
    out = r.review("f", "TITLE", BUNDLE, "q", CANDS, "L2", "L1_AQSC",
                   "RELEVANT", "r", {}, "m", supplement="补充段落XYZ")
    assert out["result_type"] == "CLASSIFIED"
    assert len(seen) == 2 and "补充段落XYZ" in seen[1] and "补充段落XYZ" not in seen[0]


def test_two_rounds_uncertain_gives_unresolved():
    r = rev(ScriptedLLM(default_choice=None))
    out = r.review("f", "TITLE", BUNDLE, "q", CANDS, "L2", "L1_AQSC",
                   "RELEVANT", "r", {}, "m")
    assert out["result_type"] == "UNRESOLVED"
    assert out["level2"] is None and out["candidates_kept"] == ["L2_WHX", "L2_ZFGW"]
    assert out["classification_basis"] and out["taxonomy_version"]


def test_new_category_rejected():
    r = rev(ScriptedLLM(new_category=True))
    out = r.review("f", "TITLE", BUNDLE, "q", CANDS, "L2", "L1_AQSC",
                   "RELEVANT", "r", {}, "m")
    assert out["result_type"] == "UNRESOLVED"
    assert "L9_NEW_MADE_UP" not in str(out)


def test_irrelevant_output():
    r = rev(ScriptedLLM(default_choice="IRRELEVANT"))
    out = r.review("f", "TITLE", BUNDLE, "q", CANDS, "L2", "L1_AQSC",
                   "UNCERTAIN", "r", {}, "m")
    assert out["result_type"] == "IRRELEVANT"


def test_ocr_insufficient_no_guessing():
    llm = ScriptedLLM(default_choice="L2_WHX")
    r = rev(llm)
    out = r.review("f", "OCR_BODY", BUNDLE, "q", CANDS, "L2", "L1_AQSC",
                   "RELEVANT", "r", {}, "m", ocr_score=0.2)
    assert out["result_type"] == "UNRESOLVED"
    assert out["suggested_action"] == "REPARSE_OR_VISION"
    assert llm.calls == []


def test_call_failure_separate_from_uncertainty():
    class Boom:
        prompt_version = "t"
        def choose(self, *a, **k):
            raise RuntimeError("endpoint down")
    r = rev(Boom(), max_retries=1)
    out = r.review("f", "TITLE", BUNDLE, "q", CANDS, "L2", "L1_AQSC",
                   "RELEVANT", "r", {}, "m")
    assert out["result_type"] == "FAILED"
    assert out["error"]["code"] == "LLM_CALL_FAILED" and out["error"]["retryable"] is True
    assert out["review_stats"]["call_failures"] >= 1


def test_retry_then_success_counted():
    class Flaky:
        prompt_version = "t"
        def __init__(self):
            self.n = 0
        def choose(self, *a, **k):
            self.n += 1
            if self.n == 1:
                raise RuntimeError("timeout once")
            return {"choice": "L2_WHX"}
    r = rev(Flaky(), max_retries=2)
    out = r.review("f", "TITLE", BUNDLE, "q", CANDS, "L2", "L1_AQSC",
                   "RELEVANT", "r", {}, "m")
    assert out["result_type"] == "CLASSIFIED"
    assert out["review_stats"]["retries"] == 1


def test_timeout_counted():
    class Slow:
        prompt_version = "t"
        def choose(self, *a, **k):
            time.sleep(0.3)
            return {"choice": "L2_WHX"}
    r = rev(Slow(), timeout_s=0.05, max_retries=0)
    out = r.review("f", "TITLE", BUNDLE, "q", CANDS, "L2", "L1_AQSC",
                   "RELEVANT", "r", {}, "m")
    assert out["result_type"] == "FAILED"
    assert out["review_stats"]["timeouts"] >= 1


def test_pipeline_conflict_auto_resolved_with_parent_constraint():
    pipe = ClassificationPipeline(KeywordEmbedding(), KeywordReranker(),
                                  ScriptedLLM(default_choice="L2_WHX"),
                                  Thresholds(calibrated=True))
    parsed = {"file_path": "x.pdf", "pdf_type": "TEXT_PDF", "evidence_mode": "TITLE",
              "title": "危险化学品企业专项执法检查通知",
              "headings": [], "pages": [{"page_no": 1, "text": "危化企业执法检查与行政处罚。"}]}
    out = pipe.run(parsed)
    assert out["result_type"] in ("CLASSIFIED", "UNRESOLVED")
    if out["result_type"] == "CLASSIFIED":
        assert out["level2"] in KB["l2_by_l1"][out["level1"]]
