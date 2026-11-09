"""台账 + 波次：灌清单 → 波次 0 → OCR → 分类 → 归档 → 复核改判，全程用 SQLite 和替身模型。"""
import json
import os
import time

import pymupdf
import pytest

from phase1 import ledger
from phase1.archive import ArchiveError, copy_into, remove_copy
from phase1.waves import run_loop

CNIPA = "(19)中华人民共和国国家知识产权局 (12)发明专利申请 (21)申请号 201911234567.8 (22)申请日 2019.12.01"


class FakeOcr:
    def recognize(self, image):
        assert image.ndim == 3 and image.shape[2] == 3
        return [("关于做好防汛抗旱物资储备工作的通知", 0.98), ("各单位要落实防汛责任。", 0.95)]


def fake_classifier(calibrated=True):
    def factory():
        def classify(text):
            if "防汛" in text:
                return {"calibrated": calibrated, "confirmed_unmatched": False,
                        "level1": "L1_FZZJ", "level2": "防汛抗旱", "reason": "clear"}
            return {"calibrated": calibrated, "confirmed_unmatched": False,
                    "level1": None, "level2": None, "reason": "relevance_uncertain"}
        return classify
    return factory


@pytest.fixture()
def env(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "patent.txt").write_text(CNIPA, encoding="utf-8")
    (src / "empty.txt").write_bytes(b"")
    (src / "flood.txt").write_text("防汛抗旱应急响应工作总结，汛期物资调度情况。", encoding="gbk")
    (src / "other.txt").write_text("食堂菜单与采购事项说明，供参考。", encoding="utf-8")
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_image(page.rect, pixmap=pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 280), 0))
    doc.save(str(src / "scan.pdf"))
    (src / "bundle.zip").write_bytes(b"PK\x03\x04xx")
    masters = tmp_path / "masters.csv"
    lines = ["完整路径,文件大小_字节,来源"] + [f"{p},{p.stat().st_size},大小唯一" for p in sorted(src.iterdir())]
    lines.append(f"{src / 'missing.txt'},10,大小唯一")
    masters.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    url = f"sqlite:///{tmp_path}/ledger.db"
    engine = ledger.make_engine(url)
    ledger.ensure_schema(engine)
    return {"tmp": tmp_path, "src": src, "masters": masters, "url": url, "engine": engine,
            "root": str(tmp_path / "archive")}


def _load(env):
    from phase1.masters import iter_master_rows
    return ledger.load_masters(env["engine"], iter_master_rows(str(env["masters"])))


def _rows(env):
    return {os.path.basename(r["source_path"]): r for r in ledger.iter_all(env["engine"])}


def _run(env, stages, **kw):
    kw.setdefault("handler_kw", {"archive_root": env["root"], "ocr_factory": FakeOcr,
                                 "classifier_factory": fake_classifier()})
    return run_loop(env["url"], stages, until_empty=True, idle_sleep=0, backoff_s=0, **kw)


def test_full_flow(env):
    assert _load(env) == {"seen": 7, "inserted": 7, "already_loaded": 0}
    assert _load(env)["inserted"] == 0

    _run(env, ["cpu"])
    rows = _rows(env)
    assert rows["patent.txt"]["stage"] == "archive" and rows["patent.txt"]["archive_type"] == "PATENT"
    assert rows["empty.txt"]["reason"] == "LV03"
    assert rows["scan.pdf"]["stage"] == "ocr" and rows["scan.pdf"]["ocr_pages"] == [0]
    assert rows["flood.txt"]["stage"] == "classify"
    assert rows["bundle.zip"]["archive_type"] == "PENDING"
    assert rows["missing.txt"]["stage"] == "failed" and "RETRYABLE_IO" in rows["missing.txt"]["last_error"]

    _run(env, ["ocr"])
    assert _rows(env)["scan.pdf"]["stage"] == "classify"
    _run(env, ["classify"])
    _run(env, ["archive"])
    rows = _rows(env)
    assert rows["scan.pdf"]["archive_type"] == "BUSINESS" and rows["scan.pdf"]["level2"] == "防汛抗旱"
    assert rows["flood.txt"]["target_dir"] == "03_业务分类/防灾减灾救灾/防汛抗旱/"
    assert rows["other.txt"]["archive_type"] == "PENDING"
    for name in ("patent.txt", "empty.txt", "flood.txt", "scan.pdf", "other.txt", "bundle.zip"):
        row = rows[name]
        assert row["stage"] == "done", (name, row)
        assert os.path.isfile(row["archive_path"]) and row["archive_path"].endswith(os.path.splitext(name)[1])
        assert open(row["archive_path"], "rb").read() == (env["src"] / name).read_bytes()
    summary = ledger.summary(env["engine"])
    assert summary["by_stage"] == {"done": 6, "failed": 1}

    # 复核：待复核的 other.txt 改判为综合办公，副本搬过去，旧副本删掉
    old = rows["other.txt"]["archive_path"]
    ledger.review(env["engine"], rows["other.txt"]["id"], reviewer="张三", archive_type="BUSINESS",
                  level1="L1_ZHGL", level2="综合办公")
    _run(env, ["archive"])
    after = _rows(env)["other.txt"]
    assert after["stage"] == "done" and "综合管理/综合办公" in after["archive_path"]
    assert not os.path.exists(old) and os.path.isfile(after["archive_path"])
    assert after["decision_status"] == "REVIEWED" and after["previous_archive_path"] is None

    with pytest.raises(ValueError):
        ledger.review(env["engine"], after["id"], reviewer="x", archive_type="BUSINESS",
                      level1="L1_AQSC", level2="综合办公")
    with pytest.raises(ValueError):
        ledger.review(env["engine"], after["id"], reviewer="x", archive_type="PENDING")

    assert ledger.requeue_failed(env["engine"]) == 1
    assert _rows(env)["missing.txt"]["stage"] == "cpu"


def test_uncalibrated_classifier_keeps_pending(env):
    _load(env)
    _run(env, ["cpu", "ocr"])
    _run(env, ["classify"], handler_kw={"classifier_factory": fake_classifier(calibrated=False)})
    row = _rows(env)["flood.txt"]
    assert row["archive_type"] == "PENDING" and row["reason"] == "uncalibrated_thresholds_blocked"
    assert row["level2"] == "防汛抗旱"


def test_crashed_lease_is_reclaimed_and_stale_owner_cannot_write(env):
    _load(env)
    engine = env["engine"]
    taken = ledger.claim(engine, "cpu", "dead-worker", 100, lease_s=0.2)
    assert len(taken) == 7
    assert ledger.claim(engine, "cpu", "other", 100, lease_s=60) == []
    time.sleep(0.3)
    again = ledger.claim(engine, "cpu", "new-worker", 100, lease_s=60)
    assert len(again) == 7 and all(r["attempts"] == 2 for r in again)
    assert ledger.finish(engine, "dead-worker", [(again[0]["id"], {"stage": "done"})]) == 0
    assert ledger.finish(engine, "new-worker", [(again[0]["id"], {"stage": "classify"})]) == 1


def test_attempts_exhausted_goes_failed(env):
    _load(env)
    engine = env["engine"]
    for _ in range(3):
        ledger.claim(engine, "cpu", "w", 100, lease_s=0.01)
        time.sleep(0.02)
    assert ledger.claim(engine, "cpu", "w", 100, lease_s=60, max_attempts=3) == []
    assert ledger.summary(engine)["by_stage"] == {"failed": 7}


def test_archive_never_overwrites(tmp_path):
    root = tmp_path / "root"
    a = tmp_path / "a" / "同名.pdf"
    b = tmp_path / "b" / "同名.pdf"
    a.parent.mkdir()
    b.parent.mkdir()
    a.write_bytes(b"%PDF a")
    b.write_bytes(b"%PDF b")
    first = copy_into(str(a), str(root), "04_待复核暂存/")
    again = copy_into(str(a), str(root), "04_待复核暂存/")
    assert again["existed"] and again["final"] == first["final"]
    # 同一个 12 位前缀的名字被另一份不同内容的文件占了：换更长的前缀，原文件不动
    squatter = os.path.join(os.path.dirname(first["final"]), os.path.basename(first["final"]))
    other = copy_into(str(b), str(root), "04_待复核暂存/")
    assert other["final"] != first["final"]
    assert open(squatter, "rb").read() == b"%PDF a"
    os.remove(other["final"])
    os.rename(first["final"], first["final"] + ".bak")
    with open(first["final"], "wb") as handle:
        handle.write(b"someone else")
    moved = copy_into(str(a), str(root), "04_待复核暂存/")
    assert moved["final"] != first["final"] and open(first["final"], "rb").read() == b"someone else"
    with pytest.raises(ArchiveError):
        remove_copy(first["final"], str(root), first["sha256"])
    with pytest.raises(ValueError):
        copy_into(str(a), str(root), "../escape/")
    with pytest.raises(ValueError):
        copy_into(squatter, str(root), "01_专利文件/")


def test_export_and_cli(env, capsys):
    from phase1.__main__ import main

    assert main(["--db", env["url"], "load", "--masters", str(env["masters"])]) == 0
    assert main(["--db", env["url"], "work", "--stage", "cpu", "--until-empty"]) == 0
    assert main(["--db", env["url"], "status"]) == 0
    out = env["tmp"] / "decisions.jsonl"
    assert main(["--db", env["url"], "export", "--out", str(out)]) == 0
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 7 and all("evidence" not in r for r in rows)
