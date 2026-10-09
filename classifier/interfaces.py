from __future__ import annotations
from typing import Protocol


class EmbeddingBackend(Protocol):
    model_version: str
    def score(self, query: str, candidates: list[str]) -> list[float]: ...


class RerankerBackend(Protocol):
    model_version: str
    def rerank(self, query: str, candidates: list[str]) -> list[float]: ...


class LLMJudge(Protocol):
    prompt_version: str
    def choose(self, evidence: str, options: list[str], allow_uncertain: bool = True) -> dict: ...
