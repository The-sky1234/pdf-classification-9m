"""RedisQueue 语义（需要本机 Redis，没有就跳过）。"""
import os
import uuid

import pytest

redis = pytest.importorskip("redis")
URL = os.environ.get("PDFCLS_TEST_REDIS", "redis://127.0.0.1:6390/15")


@pytest.fixture()
def q():
    from scheduler.queue import RedisQueue

    try:
        redis.Redis.from_url(URL).ping()
    except Exception:
        pytest.skip("没有可用的测试 Redis")
    queue = RedisQueue(URL, queue=f"test-{uuid.uuid4().hex}")
    yield queue
    queue.r.delete(queue.queue, queue.processing, queue.live_key, queue.queue + ":dead")


def test_redis_queue_ack_and_recover(q):
    assert q.enqueue("a", {"n": 1}) and not q.enqueue("a", {})
    assert q.dequeue() == ("a", {"n": 1})
    assert q.dequeue() is None
    assert q.recover() == 1
    assert q.dequeue() == ("a", {"n": 1})
    q.ack("a")
    assert q.r.llen(q.processing) == 0 and q.enqueue("a", {"n": 2})
