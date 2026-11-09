"""跨进程 GPU 互斥（文件锁 O_EXCL + 超时回收），禁止用进程内线程锁冒充。

机器只有一张 NVIDIA RTX A6000，48GB，设备号 0。OCR、MinerU、分类推理必须传同一个
group，锁文件才是 `gpu-a6000.lock`。分组不同会同时上卡，48GB 不够分给多张卡。
"""
from __future__ import annotations
import os
import time

GPU_LEASE_GROUP = "a6000"


class FileGpuLease:
    def __init__(self, lockdir: str, group: str, stale_after: float = 300.0):
        self.lockdir = lockdir
        self.group = group
        self.stale_after = stale_after
        os.makedirs(lockdir, exist_ok=True)
        self.path = os.path.join(lockdir, f"gpu-{group}.lock")
        self._held = False

    def acquire(self, timeout: float = 60.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w") as f:
                    f.write(f"{os.getpid()}:{time.time()}")
                self._held = True
                return True
            except FileExistsError:
                try:
                    with open(self.path) as f:
                        ts = float(f.read().split(":")[1])
                    if time.time() - ts > self.stale_after:
                        os.unlink(self.path)
                        continue
                except (FileNotFoundError, ValueError, IndexError):
                    continue
            time.sleep(0.05)
        return False

    def release(self):
        if self._held:
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass
            self._held = False

    def __enter__(self):
        if not self.acquire():
            raise TimeoutError(f"gpu lease timeout: {self.group}")
        return self

    def __exit__(self, *a):
        self.release()
