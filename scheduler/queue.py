"""通用任务队列（阶段一波次已改用 phase1.ledger 的数据库租约，不再依赖 Redis）。

去重只覆盖「排队中或处理中」：处理完 ack 以后，同一个 task_id 可以再次入队；
处理中进程崩溃，recover() 把未 ack 的任务放回队列，不会永久丢失。
"""
from __future__ import annotations
import json
import threading
from collections import deque


class FakeQueue:
    """内存队列：测试替身，语义与 RedisQueue 一致。"""
    def __init__(self):
        self.q: deque = deque()
        self.live: set[str] = set()
        self.inflight: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.dead: list[dict] = []

    def enqueue(self, task_id: str, payload: dict) -> bool:
        with self.lock:
            if task_id in self.live:
                return False
            self.live.add(task_id)
            self.q.append((task_id, payload))
            return True

    def dequeue(self):
        with self.lock:
            if not self.q:
                return None
            task_id, payload = self.q.popleft()
            self.inflight[task_id] = payload
            return task_id, payload

    def ack(self, task_id: str) -> None:
        with self.lock:
            self.inflight.pop(task_id, None)
            self.live.discard(task_id)

    def recover(self) -> int:
        with self.lock:
            items = list(self.inflight.items())
            self.inflight.clear()
            self.q.extendleft(reversed(items))
            return len(items)

    def qsize(self) -> int:
        with self.lock:
            return len(self.q)

    def to_dead(self, item: dict):
        with self.lock:
            self.dead.append(item)
            self.inflight.pop(item.get("task_id", ""), None)
            self.live.discard(item.get("task_id", ""))


class RedisQueue:
    """Redis ≥ 6.2（BLMOVE）。queue → queue:processing，ack 后才删。"""
    backend = "redis"

    def __init__(self, url: str, queue: str = "pdf_tasks"):
        import redis
        self.r = redis.Redis.from_url(url)
        self.queue = queue
        self.processing = queue + ":processing"
        self.live_key = queue + ":live"

    def enqueue(self, task_id: str, payload: dict) -> bool:
        if self.r.sadd(self.live_key, task_id) == 0:
            return False
        self.r.rpush(self.queue, json.dumps({"task_id": task_id, "payload": payload}))
        return True

    def dequeue(self):
        raw = self.r.blmove(self.queue, self.processing, 1, "LEFT", "RIGHT")
        if not raw:
            return None
        item = json.loads(raw)
        return item["task_id"], item["payload"]

    def ack(self, task_id: str) -> None:
        for raw in self.r.lrange(self.processing, 0, -1):
            if json.loads(raw)["task_id"] == task_id:
                self.r.lrem(self.processing, 1, raw)
                break
        self.r.srem(self.live_key, task_id)

    def recover(self) -> int:
        """只在所有消费者都停下时调用（例如启动时），把处理中未 ack 的放回队首。"""
        n = 0
        while self.r.lmove(self.processing, self.queue, "RIGHT", "LEFT"):
            n += 1
        return n

    def qsize(self) -> int:
        return self.r.llen(self.queue)

    def to_dead(self, item: dict):
        self.r.rpush(self.queue + ":dead", json.dumps(item))
        if item.get("task_id"):
            self.ack(item["task_id"])
