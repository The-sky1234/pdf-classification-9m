from __future__ import annotations
import threading
from collections import deque


class FakeQueue:
    """内存队列。queued 挡住尚未取出的重复投递，inflight 挡住处理中的重复投递。

    release 在处理结束或确认队列已丢失后调用，失败任务才能再次入队。
    """
    def __init__(self):
        self.q: deque = deque()
        self.queued: set[str] = set()
        self.inflight: set[str] = set()
        self.lock = threading.Lock()
        self.dead: list[dict] = []

    def enqueue(self, task_id: str, payload: dict) -> bool:
        with self.lock:
            if task_id in self.queued or task_id in self.inflight:
                return False
            self.queued.add(task_id)
            self.q.append((task_id, payload))
            return True

    def dequeue(self):
        with self.lock:
            if not self.q:
                return None
            task_id, payload = self.q.popleft()
            self.queued.discard(task_id)
            self.inflight.add(task_id)
            return task_id, payload

    def release(self, task_id: str) -> None:
        with self.lock:
            self.inflight.discard(task_id)
            self.queued.discard(task_id)

    def in_queue(self, task_id: str) -> bool:
        with self.lock:
            return task_id in self.queued

    def qsize(self) -> int:
        with self.lock:
            return len(self.q)

    def to_dead(self, item: dict):
        with self.lock:
            self.dead.append(item)
            task_id = item.get("task_id")
        if task_id:
            self.release(task_id)


class RedisQueue:
    backend = "redis"

    def __init__(self, url: str, queue: str = "pdf_tasks"):
        import redis
        self.r = redis.Redis.from_url(url)
        self.queue = queue
        self.queued_key = queue + ":queued"
        self.inflight_key = queue + ":inflight"

    def enqueue(self, task_id: str, payload: dict) -> bool:
        import json
        if self.r.sismember(self.queued_key, task_id) or self.r.sismember(self.inflight_key, task_id):
            return False
        if self.r.sadd(self.queued_key, task_id) == 0:
            return False
        self.r.rpush(self.queue, json.dumps({"task_id": task_id, "payload": payload}))
        return True

    def dequeue(self):
        import json
        raw = self.r.blpop(self.queue, timeout=1)
        if not raw:
            return None
        item = json.loads(raw[1])
        task_id = item["task_id"]
        pipe = self.r.pipeline()
        pipe.srem(self.queued_key, task_id)
        pipe.sadd(self.inflight_key, task_id)
        pipe.execute()
        return task_id, item["payload"]

    def release(self, task_id: str) -> None:
        pipe = self.r.pipeline()
        pipe.srem(self.inflight_key, task_id)
        pipe.srem(self.queued_key, task_id)
        pipe.execute()

    def in_queue(self, task_id: str) -> bool:
        return bool(self.r.sismember(self.queued_key, task_id))

    def qsize(self) -> int:
        return self.r.llen(self.queue)

    def to_dead(self, item: dict):
        import json
        self.r.rpush(self.queue + ":dead", json.dumps(item))
        task_id = item.get("task_id")
        if task_id:
            self.release(task_id)


def open_queue(name: str):
    """有 REDIS_URL 时走 Redis，否则用进程内队列。登记幂等不使用这里的集合。"""
    import os
    url = os.environ.get("REDIS_URL", "")
    if url:
        return RedisQueue(url, name)
    return FakeQueue()
