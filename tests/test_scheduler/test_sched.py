import threading
from scheduler.queue import FakeQueue
from scheduler.pipeline import Pipeline
from scheduler.resources import GPUPool, Throttle
from scheduler.benchmark import Sample, estimate_full_run


def test_idempotent_enqueue():
    q = FakeQueue()
    assert q.enqueue("t1", {}) is True
    assert q.enqueue("t1", {}) is False
    assert q.qsize() == 1


def test_retry_then_dead():
    calls = {"n": 0}

    def flaky(fp):
        calls["n"] += 1
        e = RuntimeError("boom")
        e.code = "TIMEOUT"
        raise e

    p = Pipeline(parse_fn=flaky)
    for _ in range(4):
        out = p.run_one("t1", "/x.pdf")
    assert out["status"] == "DEAD"
    assert p.records["t1"].retry_count == 3


def test_restart_resume():
    p = Pipeline()
    p.run_one("a", "/a.pdf")
    p.records["b"] = p.records["a"]
    from scheduler.pipeline import TaskRecord
    p.records["c"] = TaskRecord("c", "/c.pdf", status="RETRY")
    assert "c" in p.resume_pending() and "a" not in p.resume_pending()


def test_gpu_lease_serializes():
    pool = GPUPool({"ocr": 1})
    order = []
    assert pool.acquire("ocr", timeout=1)
    def worker():
        assert pool.acquire("ocr", timeout=5)
        order.append("w")
        pool.release("ocr")
    t = threading.Thread(target=worker)
    t.start()
    assert order == []
    pool.release("ocr")
    t.join()
    assert order == ["w"]


def test_backpressure_and_stop():
    th = Throttle(max_backlog=2)
    assert th.can_submit(1) and not th.can_submit(5)
    th.request_stop()
    assert not th.can_submit(0) and th.stopped()


def test_concurrent_workers():
    q = FakeQueue()
    for i in range(20):
        q.enqueue(f"t{i}", {"fp": f"/{i}.pdf"})
    done = []
    p = Pipeline(save_fn=lambda r: done.append(r["task_id"]))

    def w():
        while True:
            item = q.dequeue()
            if not item:
                return
            tid, pl = item
            p.run_one(tid, pl["fp"])

    ts = [threading.Thread(target=w) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(done) == sorted(f"t{i}" for i in range(20))


def test_benchmark_estimate():
    s = Sample(n_files=1000, n_pages=10000, seconds=600.0)
    est = estimate_full_run(s, 9_000_000)
    assert est["total_pages"] == 90_000_000 and "est_days" in est
