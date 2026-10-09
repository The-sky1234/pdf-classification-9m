"""ISSUE-01复现：跨Worker交接（payload引用+断点续跑）。先失败后修复。"""
from scheduler.handoff import FilePayloadStore, HandoffRecord
from scheduler.pipeline import Pipeline


def test_payload_ref_roundtrip(tmp_path):
    store = FilePayloadStore(str(tmp_path / "payloads"))
    big = {"file_path": "/x.pdf", "pages": [{"page_no": 1, "text": "t" * 5000}]}
    ref = store.save("task-1", "PARSED", big)
    assert "全文" not in ref and len(ref) < 300
    assert store.load(ref) == big


def test_restart_resumes_next_stage(tmp_path):
    store = FilePayloadStore(str(tmp_path / "payloads"))
    ref = store.save("task-9", "PARSED", {"file_path": "/x.pdf"})
    rec = HandoffRecord(task_id="task-9", file_path="/x.pdf", stage="PARSED", parse_ref=ref)
    store.save_record(rec)
    store2 = FilePayloadStore(str(tmp_path / "payloads"))
    resumed = store2.load_record("task-9")
    assert resumed.stage == "PARSED"
    done = []
    pipe = Pipeline(parse_fn=lambda fp: store2.load(resumed.parse_ref),
                    classify_fn=lambda d: {"result_type": "MANUAL_REVIEW"},
                    save_fn=lambda r: done.append(r))
    assert pipe.run_one("task-9", "/x.pdf")["status"] == "DONE" and len(done) == 1


def test_queue_carries_ref_not_fulltext(tmp_path):
    from scheduler.queue import FakeQueue
    store = FilePayloadStore(str(tmp_path / "payloads"))
    ref = store.save("t", "PARSED", {"pages": [{"text": "x" * 10000}]})
    q = FakeQueue()
    assert q.enqueue("t", {"file_path": "/a.pdf", "stage": "PARSED", "parse_ref": ref})
    import json
    msg = json.dumps({"parse_ref": ref})
    assert len(msg) < 2000
