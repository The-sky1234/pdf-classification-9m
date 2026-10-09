from __future__ import annotations
from dataclasses import dataclass


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
