from __future__ import annotations
import os
from fastapi import Header, HTTPException

API_KEY = os.environ.get("API_KEY", "dev-key-change-me")


def require_key(x_api_key: str | None = Header(default=None)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="unauthorized: bad api key")
    return x_api_key
