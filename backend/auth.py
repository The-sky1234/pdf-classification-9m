from __future__ import annotations

import hmac
import os

from fastapi import Header, HTTPException


def expected_key() -> str:
    """每次请求时读，.env 晚于本模块加载、或运维改了环境变量重启，都以当前值为准。"""
    return os.environ.get("API_KEY", "")


def require_key(x_api_key: str | None = Header(default=None)):
    expected = expected_key()
    if not expected:
        raise HTTPException(status_code=503, detail="API_KEY not configured on server")
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="unauthorized: bad api key")
    return x_api_key
