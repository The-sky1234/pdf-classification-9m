from __future__ import annotations
import threading
from collections import deque


class FakeQueue:
    """内存队列：Windows无Redis时的测试替身，接口与Redis/Celery投递一致。"""
    def __init__(self):
        self.q: deque = deque()
        self.seen: set[str] = set()
        self.lock = threading.Lock()
        self.dead: list[dict] = []

    def enqueue(self, task_id: str, payload: dict) -> bool:
        with self.lock:
            if task_id in self.seen:
                return False
            self.seen.add(task_id)
            self.q.append((task_id, payload))
            return True

    def dequeue(self):
        with self.lock:
            return self.q.popleft() if self.q else None

    def qsize(self) -> int:
        with self.lock:
            return len(self.q)

    def to_dead(self, item: dict):
        with self.lock:
            self.dead.append(item)


class RedisQueue:
    backend = "redis"

    def __init__(self, url: str, queue: str = "pdf_tasks"):
        import redis
        self.r = redis.Redis.from_url(url)
        self.queue = queue
        self.seen_key = queue + ":seen"

    def enqueue(self, task_id: str, payload: dict) -> bool:
        import json
        if self.r.sadd(self.seen_key, task_id) == 0:
            return False
        self.r.rpush(self.queue, json.dumps({"task_id": task_id, "payload": payload}))
        return True

    def dequeue(self):
        import json
        raw = self.r.blpop(self.queue, timeout=1)
        if not raw:
            return None
        item = json.loads(raw[1])
        return item["task_id"], item["payload"]

    def qsize(self) -> int:
        return self.r.llen(self.queue)

    def to_dead(self, item: dict):
        import json
        self.r.rpush(self.queue + ":dead", json.dumps(item))
