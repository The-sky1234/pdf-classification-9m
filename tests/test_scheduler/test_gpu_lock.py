"""ISSUE-09复现：跨进程GPU互斥（文件锁，不是进程内线程锁）。"""
import multiprocessing as mp
import os
import time

from scheduler.gpu_lock import FileGpuLease


def _holder(lockdir, group, hold, out):
    lease = FileGpuLease(lockdir, group)
    if lease.acquire(timeout=10):
        out.append("in")
        time.sleep(hold)
        lease.release()


def _crash_holding(lockdir, group, ready):
    lease = FileGpuLease(lockdir, group)
    assert lease.acquire(timeout=5)
    ready.set()
    time.sleep(0.3)
    os._exit(1)


def test_cross_process_mutual_exclusion(tmp_path):
    mgr = mp.Manager()
    out = mgr.list()
    p1 = mp.Process(target=_holder, args=(str(tmp_path), "ocr", 2.0, out))
    p1.start()
    time.sleep(0.5)
    lease2 = FileGpuLease(str(tmp_path), "ocr")
    assert lease2.acquire(timeout=0.5) is False
    p1.join()
    assert lease2.acquire(timeout=5) is True
    lease2.release()


def test_crash_releases_lock(tmp_path):
    ready = mp.Event()
    p = mp.Process(target=_crash_holding, args=(str(tmp_path), "infer", ready))
    p.start()
    assert ready.wait(5)
    lease = FileGpuLease(str(tmp_path), "infer")
    assert lease.acquire(timeout=0.1) is False
    p.join()
    assert lease.acquire(timeout=2) is True
    lease.release()


def test_long_holder_is_not_stolen(tmp_path):
    """旧实现 300 秒后会把还在用卡的持有者当成过期锁删掉。现在只要进程活着就一直持有。"""
    mgr = mp.Manager()
    out = mgr.list()
    p = mp.Process(target=_holder, args=(str(tmp_path), "a6000", 3.0, out))
    p.start()
    time.sleep(0.5)
    os.utime(os.path.join(str(tmp_path), "gpu-a6000.lock"), (0, 0))
    lease = FileGpuLease(str(tmp_path), "a6000")
    assert lease.acquire(timeout=1.0) is False
    p.join()
    assert lease.acquire(timeout=2) is True
    lease.release()
