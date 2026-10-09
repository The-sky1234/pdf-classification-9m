from __future__ import annotations
import threading
import time


class GPUPool:
    """GPU资源租约：OCR/MinerU/BGE/LLM按卡分组串行，防无序抢占。"""

    def __init__(self, groups: dict[str, int] | None = None):
        groups = groups or {"ocr": 2, "mineru": 1, "infer": 1}
        self.sems = {k: threading.Semaphore(v) for k, v in groups.items()}
        self.in_use: dict[str, int] = {k: 0 for k in groups}
        self.lock = threading.Lock()

    def acquire(self, group: str, timeout: float = 60.0) -> bool:
        sem = self.sems[group]
        ok = sem.acquire(timeout=timeout)
        if ok:
            with self.lock:
                self.in_use[group] += 1
        return ok

    def release(self, group: str):
        with self.lock:
            self.in_use[group] = max(0, self.in_use[group] - 1)
        self.sems[group].release()

    def stats(self) -> dict:
        with self.lock:
            return dict(self.in_use)


class Throttle:
    """限流背压：队列积压超阈值暂停投递；优雅停止标志。"""

    def __init__(self, max_backlog: int = 10000):
        self.max_backlog = max_backlog
        self.stop_flag = threading.Event()

    def can_submit(self, backlog: int) -> bool:
        return backlog < self.max_backlog and not self.stop_flag.is_set()

    def request_stop(self):
        self.stop_flag.set()

    def stopped(self) -> bool:
        return self.stop_flag.is_set()
