import json

from classifier.backends import LLMHttpBackend
from classifier.config import Thresholds
from classifier.features import build
from classifier.scene_kb import L1_TEXT, SCENE_TEXT
from classifier.taxonomy_v3 import L1, L2
from classifier.v3_label import judge_v3, make_runtime_classifier
from exclusion.flow import decide


def _parse(text: str = "abcdefghijklmnop") -> dict:
    return {"ok": True, "file_path": "a.txt", "pdf_type": "TEXT_PDF", "title": text,
            "headings": [], "evidence_mode": "BODY",
            "pages": [{"page_no": 1, "text": text}]}


class _Emb:
    model_version = "BAAI/bge-m3"

    def __init__(self, scores):
        self.scores = scores
        self.calls = 0

    def score(self, query, candidates):
        self.calls += 1
        return [self.scores(query, item) for item in candidates]


class _Rerank:
    model_version = "BAAI/bge-reranker-v2-m3"

    def __init__(self, scores):
        self.scores = scores
        self.calls = 0

    def rerank(self, query, candidates):
        self.calls += 1
        return [self.scores(query, item) for item in candidates]


class _LLM:
    prompt_version = "llm-judge-v1"

    def __init__(self, choice):
        self.choice = choice
        self.calls = []

    def choose(self, evidence, options, allow_uncertain=True):
        self.calls.append(options)
        return {"choice": self.choice, "reason": "scripted"}


def test_model_reads_only_the_first_five_pages():
    pages = [{"page_no": index, "text": f"第{index}页正文内容足够用来分类。"} for index in range(1, 8)]
    feature = build({"ok": True, "title": "封面", "evidence_mode": "BODY", "pages": pages})
    assert "第5页" in feature["query_text"]
    assert "第6页" not in feature["query_text"] and "第7页" not in feature["query_text"]
    assert [item["page_no"] for item in feature["page_evidence"]] == [1, 2, 3, 4, 5]


def test_scene_knowledge_covers_the_27_names():
    assert set(L1_TEXT) == set(L1)
    assert set(SCENE_TEXT) == {name for names in L2.values() for name in names}


def test_clear_reranker_does_not_call_the_llm():
    emb = _Emb(lambda _query, text: 0.91 if "消防监管" in text else 0.12)
    reranker = _Rerank(lambda _query, text: 0.88 if "消防监管" in text else 0.20)
    llm = _LLM("纪检监察")
    out = judge_v3(_parse(), emb, reranker, llm, Thresholds(calibrated=True))
    assert out["result_type"] == "CLASSIFIED"
    assert out["level1"] == "L1_AQSC" and out["level2"] == "消防监管"
    assert out["decision_source"] == "RERANKER_DIRECT"
    assert out["model_version"] == "BAAI/bge-m3+BAAI/bge-reranker-v2-m3"
    assert out["calibrated"] is True
    assert llm.calls == []
    assert reranker.calls == 1


def test_close_scores_ask_the_llm_once():
    emb = _Emb(lambda _query, text: 0.80 if text.startswith("安全生产") else 0.40)
    reranker = _Rerank(lambda _query, _text: 0.40)
    llm = _LLM("消防监管")
    out = judge_v3(_parse(), emb, reranker, llm, Thresholds(calibrated=True))
    assert out["decision_source"] == "LLM_JUDGE"
    assert out["level2"] == "消防监管"
    assert len(llm.calls) == 1
    assert "消防监管" in llm.calls[0]
    assert "UNCERTAIN" not in llm.calls[0]


def test_uncertain_llm_stays_in_review():
    emb = _Emb(lambda _query, text: 0.80 if text.startswith("安全生产") else 0.40)
    reranker = _Rerank(lambda _query, _text: 0.40)
    llm = _LLM("UNCERTAIN")
    out = judge_v3(_parse(), emb, reranker, llm, Thresholds(calibrated=True))
    assert out["result_type"] == "MANUAL_REVIEW"
    assert out["reason"] == "llm_uncertain"
    assert out["level2"] == "消防监管" or out["level2"] in L2["L1_AQSC"]


def test_low_scores_are_irrelevant_only_after_calibration():
    emb = _Emb(lambda _query, _text: 0.02)
    reranker = _Rerank(lambda _query, _text: 0.99)
    llm = _LLM("消防监管")
    held = judge_v3(_parse(), emb, reranker, llm, Thresholds(calibrated=False))
    assert held["result_type"] == "IRRELEVANT"
    assert held["confirmed_unmatched"] is False
    assert reranker.calls == 0 and llm.calls == []
    signed = judge_v3(_parse(), emb, reranker, llm, Thresholds(calibrated=True))
    assert signed["confirmed_unmatched"] is True
    decision = decide("f", _parse(), classify_fn=lambda parse: signed)
    assert decision["archive_type"] == "LOW_VALUE" and decision["reason"] == "LV04"
    assert decision["model_version"].startswith("BAAI/bge-m3")


def test_business_decision_keeps_the_model_name():
    emb = _Emb(lambda _query, text: 0.91 if "消防监管" in text else 0.12)
    reranker = _Rerank(lambda _query, text: 0.88 if "消防监管" in text else 0.20)
    out = judge_v3(_parse(), emb, reranker, _LLM("不会被调用"), Thresholds(calibrated=True))
    decision = decide("f", _parse(), classify_fn=lambda _parse: out)
    assert decision["archive_type"] == "BUSINESS"
    assert decision["level2"] == "消防监管"
    assert decision["decision_source"] == "RERANKER_DIRECT"
    assert decision["model_version"] == "BAAI/bge-m3+BAAI/bge-reranker-v2-m3"


def test_default_runtime_asks_for_the_real_models(monkeypatch):
    monkeypatch.delenv("CLASSIFIER_BACKEND", raising=False)
    try:
        made = make_runtime_classifier()
    except RuntimeError as exc:
        assert "bge-m3" in str(exc) or "bge-reranker" in str(exc)
        return
    assert "bge-m3" in made.model_version
    assert not made.model_version.startswith("mock")


def test_llm_http_backend_reads_the_json_choice(monkeypatch):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content":
                "```json\n{\"choice\":\"消防监管\",\"reason\":\"主责是防火监督\"}\n```"}}]}).encode()

    monkeypatch.setenv("LLM_MODEL", "Qwen2.5-7B-Instruct")
    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout=30: _Resp())
    out = LLMHttpBackend("http://gpu-llm:8001/v1").choose("证据", ["消防监管", "纪检监察"])
    assert out["choice"] == "消防监管"
    assert "防火监督" in out["reason"]
