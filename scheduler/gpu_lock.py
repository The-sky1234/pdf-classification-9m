"""跨进程 GPU 互斥，禁止用进程内线程锁冒充。

机器只有一张 NVIDIA RTX A6000，48GB，设备号 0。OCR、分类推理必须传同一个
group，锁文件才是 `gpu-a6000.lock`。分组不同会同时上卡，48GB 不够分。

用操作系统的文件锁（Linux flock / Windows msvcrt）：持有者进程退出或崩溃时内核自动释放，
不需要「超时回收」，也就不会出现长任务还在跑、锁却被别人当成过期锁抢走的情况。
锁目录必须在所有 GPU 容器之间共享（compose 里的 workdata 卷）。
"""
from __future__ import annotations

import os
import time

GPU_LEASE_GROUP = "a6000"

try:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
except ImportError:  # Windows 开发机
    import msvcrt

    def _try_lock(fd: int) -> bool:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


class FileGpuLease:
    def __init__(self, lockdir: str, group: str):
        self.lockdir = lockdir
        self.group = group
        os.makedirs(lockdir, exist_ok=True)
        self.path = os.path.join(lockdir, f"gpu-{group}.lock")
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self, timeout: float = 60.0) -> bool:
        if self._fd is not None:
            return True
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o666)
        deadline = time.monotonic() + timeout
        while True:
            if _try_lock(fd):
                os.ftruncate(fd, 0)
                os.write(fd, f"{os.getpid()}:{time.time():.0f}\n".encode())
                self._fd = fd
                return True
            if time.monotonic() >= deadline:
                os.close(fd)
                return False
            time.sleep(0.1)

    def holder(self) -> str:
        try:
            with open(self.path, encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError:
            return ""

    def release(self):
        if self._fd is None:
            return
        try:
            _unlock(self._fd)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self):
        if not self.acquire():
            raise TimeoutError(f"gpu lease timeout: {self.group} held by {self.holder() or '?'}")
        return self

    def __exit__(self, *a):
        self.release()
