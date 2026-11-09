"""波次 0 和归档。不加载 GPU 模型。"""
import json
import os
import zipfile

from phase1.archive import copy_settled
from phase1.cpu import run_cpu_wave
from phase1.judge import judge_file


def _docx(path, text):
    xml = (
        '<?xml version="1.0"?>'
        '<document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:p><w:t>{text}</w:t></w:p></document>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", xml.encode("utf-8"))


def test_rules_and_waves(tmp_path):
    empty = tmp_path / "empty.txt"
    empty.write_bytes(b"")
    meeting = tmp_path / "notice.txt"
    meeting.write_text("关于召开消防安全会议的通知，请各部门参加。", encoding="utf-8")
    patent = tmp_path / "patent.txt"
    patent.write_text("发明专利 申请公布号 CN12345678A 说明书正文足够长。", encoding="utf-8")
    image = tmp_path / "scan.png"
    image.write_bytes(b"\x89PNG\r\n")
    packed = tmp_path / "bundle.zip"
    packed.write_bytes(b"PK\x03\x04not-a-real-zip-but-nonzero")
    docx = tmp_path / "note.docx"
    _docx(docx, "这是一份关于危险化学品监管的办公文档，正文足够用来分类。")

    assert judge_file(str(empty)).reason == "LV03"
    assert judge_file(str(empty)).target_dir.endswith("无效文件/")

    held = judge_file(str(meeting))
    assert held.settled is False and held.next_wave == "classify"

    patented = judge_file(str(patent))
    assert patented.archive_type == "PATENT" and patented.settled

    assert judge_file(str(image)).next_wave == "ocr"
    assert judge_file(str(packed)).reason == "closed_type"
    assert judge_file(str(docx)).next_wave == "classify"


def test_calibrated_business_copies_original_bytes(tmp_path):
    src = tmp_path / "fire.txt"
    src.write_text("危险化学品监管检查记录，正文足够长，用于业务分类。", encoding="utf-8")

    def classify(_text):
        return {"calibrated": True, "confirmed_unmatched": False,
                "level1": "L1_AQSC", "level2": "危险化学品监管", "reason": "clear"}

    decision = judge_file(str(src), classify_fn=classify)
    assert decision.archive_type == "BUSINESS"
    assert decision.target_dir == "03_业务分类/安全生产/危险化学品监管/"
    copied = copy_settled(str(src), str(tmp_path / "archive"), decision)
    assert os.path.isfile(copied["final"])
    assert copied["final"].endswith(".txt")
    assert open(copied["final"], "rb").read() == src.read_bytes()


def test_uncalibrated_does_not_enter_business_dir(tmp_path):
    src = tmp_path / "fire.txt"
    src.write_text("危险化学品监管检查记录，正文足够长，用于业务分类。", encoding="utf-8")

    def classify(_text):
        return {"calibrated": False, "level1": "L1_AQSC", "level2": "危险化学品监管",
                "reason": "uncalibrated_thresholds_blocked"}

    decision = judge_file(str(src), classify_fn=classify)
    assert decision.archive_type == "PENDING"
    assert decision.target_dir.endswith("04_待复核暂存/")


def test_cpu_wave_writes_jsonl_and_archives_only_settled(tmp_path):
    patent = tmp_path / "p.txt"
    patent.write_text("实用新型专利 授权公告号 CN87654321B 说明书正文。", encoding="utf-8")
    meeting = tmp_path / "m.txt"
    meeting.write_text("请参加防汛会商，正文足够长，讨论物资调度。", encoding="utf-8")
    masters = tmp_path / "masters.txt"
    masters.write_text(f"{patent}\n\n# comment\n{meeting}\n", encoding="utf-8")
    out = tmp_path / "out" / "decisions.jsonl"
    root = tmp_path / "archive"
    counts = run_cpu_wave(str(masters), str(out), str(root))
    assert counts["settled"] == 1
    assert counts["classify"] == 1
    assert counts["archived"] == 1
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["archive_type"] == "PATENT"
    assert rows[1]["next_wave"] == "classify"
    assert "archive" not in rows[1]
    archived = os.listdir(os.path.join(root, "01_专利文件"))
    assert len(archived) == 1 and archived[0].endswith(".txt")
