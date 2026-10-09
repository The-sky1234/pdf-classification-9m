"""ISSUE-09复现：跨进程GPU互斥（文件锁，不是进程内线程锁）。"""
import multiprocessing as mp
from scheduler.gpu_lock import FileGpuLease


def _holder(lockdir, group, hold, out):
    lease = FileGpuLease(lockdir, group)
    if lease.acquire(timeout=10):
        out.append("in")
        import time
        time.sleep(hold)
        lease.release()


def test_cross_process_mutual_exclusion(tmp_path):
    mgr = mp.Manager()
    out = mgr.list()
    p1 = mp.Process(target=_holder, args=(str(tmp_path), "ocr", 2.0, out))
    p1.start()
    import time
    time.sleep(0.5)
    lease2 = FileGpuLease(str(tmp_path), "ocr")
    assert lease2.acquire(timeout=0.5) is False
    p1.join()
    assert lease2.acquire(timeout=5) is True
    lease2.release()


def test_crash_releases_via_timeout(tmp_path):
    import os
    lease = FileGpuLease(str(tmp_path), "infer", stale_after=0.2)
    assert lease.acquire(timeout=1) is True
    import time
    time.sleep(0.3)
    lease2 = FileGpuLease(str(tmp_path), "infer", stale_after=0.2)
    assert lease2.acquire(timeout=1) is True
    lease2.release()
