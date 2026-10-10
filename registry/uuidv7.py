"""RFC 9562 UUIDv7，供新的 file_id 使用。

生成输入不含路径、不含哈希、不含 NAMESPACE_URL。不调用 uuid.uuid5、uuid4、uuid1。
"""
from __future__ import annotations

import secrets
import time

V7_RE_TEXT = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def format_uuid_v7(ts_ms: int, rand_a: int, rand_b: int) -> str:
    if not 0 <= ts_ms < 2**48:
        raise ValueError("unix_ts_ms out of range")
    if not 0 <= rand_a < 4096:
        raise ValueError("rand_a out of range")
    if not 0 <= rand_b < 2**62:
        raise ValueError("rand_b out of range")
    value = (ts_ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    text = f"{value:032x}"
    return f"{text[:8]}-{text[8:12]}-{text[12:16]}-{text[16:20]}-{text[20:]}"


def timestamp_prefix(file_id: str) -> str:
    """48 位时间戳在规范字符串里的前 13 个字符（8-4）。"""
    return file_id[:13]


class UuidV7Factory:
    """同一进程内单调：时钟回拨则等待；同一毫秒则递增 12 位 rand_a。"""

    def __init__(self, clock_ms=None, sleep=None, randbits=None):
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._sleep = sleep or time.sleep
        self._randbits = randbits or secrets.randbits
        self._last_ms = -1
        self._rand_a = -1
        self._lock = __import__("threading").Lock()

    def new(self) -> str:
        with self._lock:
            ms = self._clock_ms()
            if ms < self._last_ms:
                ms = self._wait_until(self._last_ms)
            if ms == self._last_ms:
                if self._rand_a >= 4095:
                    ms = self._wait_until(self._last_ms + 1)
                    rand_a = 0
                else:
                    rand_a = self._rand_a + 1
            else:
                rand_a = self._randbits(12)
            rand_b = self._randbits(62)
            self._last_ms = ms
            self._rand_a = rand_a
            return format_uuid_v7(ms, rand_a, rand_b)

    def _wait_until(self, target: int) -> int:
        while True:
            ms = self._clock_ms()
            if ms >= target:
                return ms
            self._sleep(0.001)
