"""波次 2 用替身向量模型，确认校准开关会拦住自动进业务目录。"""
from classifier.config import Thresholds
from phase1.judge import judge_file
from phase1.scenes import make_scene_classifier


class _Emb:
    model_version = "test-emb"

    def score(self, query, candidates):
        if not any(word in query for word in ("安全", "消防", "危险化学")):
            return [0.0 for _ in candidates]
        return [0.95 if ("安全生产" in item or "消防" in item) else 0.01 for item in candidates]


class _Rerank:
    def rerank(self, query, candidates):
        return [0.95 if "消防监管" in item else 0.01 for item in candidates]


class _LLM:
    def choose(self, *args, **kwargs):
        raise AssertionError("分差足够时不应调用大模型")


def test_scene_classifier_respects_calibration(tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("安全生产领域的消防监管检查记录，正文足够长。", encoding="utf-8")
    blocked = make_scene_classifier(_Emb(), _Rerank(), _LLM(), Thresholds(calibrated=False))
    pending = judge_file(str(src), classify_fn=blocked)
    assert pending.archive_type == "PENDING"

    opened = make_scene_classifier(_Emb(), _Rerank(), _LLM(), Thresholds(calibrated=True))
    done = judge_file(str(src), classify_fn=opened)
    assert done.archive_type == "BUSINESS"
    assert done.level2 == "消防监管"
    assert done.target_dir == "03_业务分类/安全生产/消防监管/"
