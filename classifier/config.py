from __future__ import annotations
import json
import os
from dataclasses import dataclass, fields


@dataclass
class Thresholds:
    """所有阈值可配置。calibrated=False（默认）时禁止自动归档，
    任何自动终态一律降为MANUAL_REVIEW；生产值须首批标注+评审后签署，
    并以 calibrated=True 显式启用（见ISSUE-07门禁）。"""
    relevance_relevant: float = 0.25
    relevance_irrelevant: float = 0.15
    l1_direct_margin: float = 0.03
    l1_direct_min: float = 0.22
    l2_direct_margin: float = 0.02
    l2_direct_min: float = 0.22
    vision_direct_min_boost: float = 0.10
    tbd_direct_margin_boost: float = 0.08
    max_evidence_chars: int = 6000
    max_body_paras: int = 6
    calibrated: bool = False

    @classmethod
    def load(cls, path: str | None = None) -> "Thresholds":
        """从 JSON 文件读阈值（THRESHOLDS_FILE）。没有文件就用默认值，calibrated 保持 False。

        文件里写 "calibrated": true 才会放行自动进业务目录；这个文件应当随标注评审结论一起签发。
        """
        path = path if path is not None else os.environ.get("THRESHOLDS_FILE", "")
        if not path:
            return cls()
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ValueError(f"阈值文件里有不认识的键：{unknown}")
        return cls(**data)
