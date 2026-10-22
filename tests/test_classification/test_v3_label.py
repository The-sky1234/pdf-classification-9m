from classifier.backends import KeywordEmbedding
from classifier.config import Thresholds
from classifier.v3_label import label_v3


def _parse(text: str) -> dict:
    return {"ok": True, "file_path": "a.txt", "pdf_type": "TEXT_PDF", "title": text,
            "headings": [], "evidence_mode": "BODY",
            "pages": [{"page_no": 1, "text": text}]}


def test_one_scene_name_labels_that_scene():
    out = label_v3(_parse("消防监管检查通知。各单位排查隐患。"), KeywordEmbedding(), Thresholds())
    assert out["level1"] == "L1_AQSC" and out["level2"] == "消防监管"
    assert out["result_type"] == "CLASSIFIED" and out["calibrated"] is False


def test_two_scene_names_stay_in_review():
    out = label_v3(_parse("消防监管移交纪检监察。"), KeywordEmbedding(), Thresholds())
    assert out["result_type"] == "MANUAL_REVIEW"
    assert out["reason"] == "multiple_scene_names"


def test_unrelated_text_is_not_forced():
    out = label_v3(_parse("abcdefghijklmnop"), KeywordEmbedding(), Thresholds(calibrated=True))
    assert out["result_type"] == "MANUAL_REVIEW"
    assert out["calibrated"] is False
