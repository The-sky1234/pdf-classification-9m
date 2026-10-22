from __future__ import annotations
import os
import time
import uuid


def uuid7() -> str:
    """RFC 9562 UUIDv7，36 位小写、带连字符。输入不含路径或哈希。"""
    ts = int(time.time() * 1000) & ((1 << 48) - 1)
    raw = bytearray(os.urandom(16))
    raw[0:6] = ts.to_bytes(6, "big")
    raw[6] = 0x70 | (raw[6] & 0x0F)
    raw[8] = 0x80 | (raw[8] & 0x3F)
    return str(uuid.UUID(bytes=bytes(raw)))
