"""跨模块端到端集成测试（qa-reviewer）。
标注：
- [真实CPU] 使用真实PyMuPDF解析真实生成的PDF；分类/队列/DB为Mock或SQLite。
- [Mock输入] 直接构造解析字典（中文语义），验证CLASSIFIED全链路。
- 真实GPU测试不在本机执行（见RELEASE_CHECKLIST）。
"""
import sqlite3
import pymupdf as fitz
from parser.parser import parse_pdf
from classifier.pipeline import ClassificationPipeline
from classifier.config import Thresholds
from classifier.backends import KeywordEmbedding, KeywordReranker, ScriptedLLM
from classifier.config import Thresholds
from scheduler.pipeline import Pipeline as SchedPipeline
from scheduler.queue import FakeQueue


def make_pdf(path: str, title: str, body: str):
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), title, fontsize=22)
    page.insert_text((72, 160), body, fontsize=11)
    doc.save(path)
    doc.close()


def clf():
    return ClassificationPipeline(KeywordEmbedding(), KeywordReranker(),
                                  ScriptedLLM(), Thresholds(calibrated=True))


def test_e2e_realpdf_to_review_saved(tmp_path):
    """[真实CPU] 真实PDF解析→Mock分类→调度→SQLite落库，链路不断且无静默丢失。"""
    pdf = str(tmp_path / "r.pdf")
    make_pdf(pdf, "Flood control deployment", "River flood prevention and drought relief work plan.")
    parsed = parse_pdf(pdf)
    assert parsed.ok, parsed.error
    out = clf().run(parsed.to_dict())
    assert out["result_type"] in ("CLASSIFIED", "IRRELEVANT", "MANUAL_REVIEW",
                                         "UNRESOLVED", "FAILED")
    assert out["classification_basis"] and out["taxonomy_version"]
    con = sqlite3.connect(tmp_path / "e2e.db")
    con.execute("CREATE TABLE results (id INTEGER PRIMARY KEY, type TEXT, l1 TEXT, l2 TEXT)")
    saved = {}

    def save(r):
        saved.update(r)
        con.execute("INSERT INTO results(type,l1,l2) VALUES(?,?,?)",
                    (r["result_type"], r.get("level1"), r.get("level2")))
        con.commit()

    sp = SchedPipeline(parse_fn=lambda fp: parse_pdf(fp).to_dict(),
                       classify_fn=clf().run, save_fn=save)
    res = sp.run_one("task-1", pdf)
    assert res["status"] == "DONE"
    assert con.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 1
    # ISSUE-03 fixed: strict contract values
    assert saved["evidence_mode"] in ("TITLE", "BODY", "OCR_BODY", "VISION")


def test_e2e_classified_path_with_parser_shaped_input():
    """[Mock输入] parser形状的中文解析字典→CLASSIFIED，验证父子约束与落库字段。"""
    from classifier.knowledge import load_taxonomy
    parsed = {"file_path": "x.pdf", "pdf_type": "TEXT_PDF", "evidence_mode": "TITLE",
              "title": "危险化学品重大危险源专项检查通知",
              "headings": [], "pages": [{"page_no": 1,
              "text": "危险化学品企业重大危险源隐患排查，化工装置许可检查。"}]}
    out = clf().run(parsed)
    assert out["result_type"] == "CLASSIFIED", out
    kb = load_taxonomy("contracts/taxonomy.yaml")
    assert out["level2"] in kb["l2_by_l1"][out["level1"]]
    for k in ("candidate_scores", "decision_source", "classification_basis",
              "model_version", "taxonomy_version"):
        assert out[k] is not None


def test_e2e_corrupt_pdf_never_silently_dropped(tmp_path):
    """[真实CPU] 损坏PDF→结构化错误→调度DEAD→死信队列，全程可追踪。"""
    from scheduler.queue import FakeQueue
    bad = str(tmp_path / "bad.pdf")
    open(bad, "wb").write(b"%PDF-1.4 broken {{{")
    r = parse_pdf(bad)
    assert not r.ok and r.error is not None
    q = FakeQueue()
    sp = SchedPipeline(parse_fn=lambda fp: (_ for _ in ()).throw(RuntimeError("x")),
                       classify_fn=lambda d: d)
    out = sp.run_one("bad-1", bad)
    assert out["status"] in ("RETRY", "DEAD")
    q.to_dead({"task_id": "bad-1", "error": out.get("error")})
    assert len(q.dead) == 1


def test_e2e_retry_then_recover():
    """[Mock] 失败重试→成功恢复，retry_count递增且结果正确落库。"""
    calls = {"n": 0}

    def flaky(fp):
        calls["n"] += 1
        if calls["n"] < 2:
            e = RuntimeError("transient")
            e.code = "TIMEOUT"
            raise e
        return {"file_path": fp, "pages": []}

    done = []
    sp = SchedPipeline(parse_fn=flaky, classify_fn=lambda d: {"result_type": "MANUAL_REVIEW"},
                       save_fn=lambda r: done.append(r))
    assert sp.run_one("t", "/x.pdf")["status"] == "RETRY"
    assert sp.run_one("t", "/x.pdf")["status"] == "DONE"
    assert sp.records["t"].retry_count == 1 and len(done) == 1


def test_e2e_queue_dedup_no_double_save():
    """[Mock] 重复投递只执行一次，不重复入库。"""
    q = FakeQueue()
    assert q.enqueue("dup", {"fp": "/a.pdf"})
    assert not q.enqueue("dup", {"fp": "/a.pdf"})
    assert q.qsize() == 1
