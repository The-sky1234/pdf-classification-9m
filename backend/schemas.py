from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field


class ScanRequest(BaseModel):
    root: str = Field(..., min_length=1)
    limit: int = Field(default=1000, ge=1, le=100000)
    batch_id: Optional[int] = None


class ReviewPatch(BaseModel):
    level1: Optional[str] = None
    level2: Optional[str] = None
    reviewer: str = Field(..., min_length=1)
    note: str = ""
    decision: Optional[str] = None
    reason: Optional[str] = None
    expected_version: Optional[int] = None


class ErrorOut(BaseModel):
    code: str
    message: str
