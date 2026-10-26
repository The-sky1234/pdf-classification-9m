import json
import os
from backend.models import init_db, make_engine, make_session
from scheduler.scan_classify import run
from scripts.classify_documents import main


def _db(tmp_path, name="scan.db"):
    engine = make_engine(f"sqlite:///{tmp_path}/{name}")
    init_db(engine)
    return make_session(engine)()


def _corpus(root):
    root.mkdir()
    notice = "消防监管检查通知。各单位排查隐患并书面整改。"
    (root / "fire.txt").write_text(notice, encoding="utf-8")
    (root / "copy.txt").write_text(notice, encoding="utf-8")
    (root / "patent.txt").write_text(
        "发明专利说明书\n申请公布号 CN112233445A 本发明涉及一种救援装备。", encoding="utf-8")
    (root / "empty.txt").write_bytes(b"")
    (root / "photo.png").write_bytes(b"\x89PNG\r\n\x1a\nxx")
    (root / "skip.tmp").write_text("ignore", encoding="utf-8")


def test_scan_classifies_each_kind_and_skips_on_rerun(tmp_path, monkeypatch):
    monkeypatch.setenv("DEDUP_ALREADY_DONE", "1")
    monkeypatch.setenv("CLASSIFIER_BACKEND", "keyword")
    monkeypatch.setenv("THRESHOLDS_CALIBRATED", "0")
    src = tmp_path / "src"
    _corpus(src)
    archive = tmp_path / "archive"
    db = _db(tmp_path)
    first = run(db, str(src), str(archive), limit=10, calibrated=False)
    assert first["counts"]["BUSINESS"] == 2
    assert first["counts"]["REVIEW"] == 1
    assert first["counts"]["PATENT"] == 1
    assert first["counts"]["LOW_VALUE"] == 1
    paths = []
    for dp, _dirs, names in os.walk(archive):
        paths.extend(os.path.join(dp, name) for name in names)
    assert any("01_专利文件" in p and p.endswith(".txt") for p in paths)
    assert any("无效文件" in p and p.endswith(".txt") for p in paths)
    assert any("04_待复核暂存" in p and p.endswith(".png") for p in paths)
    assert any("03_业务分类" in p and "fire.txt" in p for p in paths)
    assert any("03_业务分类" in p and "copy.txt" in p for p in paths)
    second = run(db, str(src), str(archive), limit=10, calibrated=False)
    assert second["counts"] == {"SKIP": 5}


def test_bad_files_do_not_stop_the_batch(tmp_path, monkeypatch):
    monkeypatch.setenv("DEDUP_ALREADY_DONE", "1")
    monkeypatch.setenv("CLASSIFIER_BACKEND", "keyword")
    src = tmp_path / "src"
    src.mkdir()
    (src / "fire.txt").write_text("消防监管检查通知。各单位排查隐患。", encoding="utf-8")
    (src / "broken.pdf").write_bytes(b"%PDF-1.4 not a real pdf {{{")
    (src / "notes.bin").write_bytes(b"\x00\x01not-a-document")
    (src / "empty.txt").write_bytes(b"")
    archive = tmp_path / "archive"
    db = _db(tmp_path, "bad.db")
    out = run(db, str(src), str(archive), limit=10)
    assert out["counts"]["BUSINESS"] == 1
    assert out["counts"]["LOW_VALUE"] == 2
    assert "ERROR" not in out["counts"]
    names = []
    for dp, _dirs, files in os.walk(archive):
        names.extend(os.path.join(dp, name) for name in files)
    assert any("03_业务分类" in path and path.endswith("fire.txt") for path in names)
    assert any("无效文件" in path and path.endswith("broken.pdf") for path in names)
    assert not any(path.endswith("notes.bin") for path in names)


def test_calibrated_scene_keeps_extension(tmp_path, monkeypatch):
    monkeypatch.setenv("DEDUP_ALREADY_DONE", "1")
    monkeypatch.setenv("CLASSIFIER_BACKEND", "keyword")
    src = tmp_path / "src"
    src.mkdir()
    (src / "fire.txt").write_text("消防监管检查通知。各单位排查隐患。", encoding="utf-8")
    archive = tmp_path / "archive"
    db = _db(tmp_path, "cal.db")
    out = run(db, str(src), str(archive), calibrated=True)
    assert out["counts"] == {"BUSINESS": 1}
    found = []
    for dp, _dirs, names in os.walk(archive):
        found.extend(os.path.join(dp, name) for name in names)
    assert len(found) == 1
    assert found[0].endswith(".txt") and "消防监管" in found[0]


def test_cli_rejects_archive_inside_source(tmp_path, capsys):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("消防监管检查通知。", encoding="utf-8")
    code = main(["--root", str(src), "--archive", str(src / "out"),
                 "--db", f"sqlite:///{tmp_path}/cli.db"])
    assert code == 2
    assert "inside" in capsys.readouterr().err


def test_cli_prints_summary(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("DEDUP_ALREADY_DONE", "1")
    monkeypatch.setenv("CLASSIFIER_BACKEND", "keyword")
    monkeypatch.setenv("THRESHOLDS_CALIBRATED", "0")
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("消防监管检查通知。", encoding="utf-8")
    code = main(["--root", str(src), "--archive", str(tmp_path / "archive"),
                 "--db", f"sqlite:///{tmp_path}/cli2.db", "--limit", "5"])
    assert code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["counts"]["BUSINESS"] == 1 and summary["batch_id"]
