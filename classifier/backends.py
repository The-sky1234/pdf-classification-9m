from __future__ import annotations


class KeywordEmbedding:
    """Mock embedding：候选描述与query的字符二元重叠率；仅用于Windows开发测试。"""
    model_version = "mock-keyword-1.0"

    def score(self, query: str, candidates: list[str]) -> list[float]:
        def grams(s: str) -> set[str]:
            s = "".join(s.split())
            return {s[i:i + 2] for i in range(max(0, len(s) - 1))} or {s}
        q = grams(query)
        out = []
        for c in candidates:
            g = grams(c)
            out.append(len(q & g) / max(1, len(q | g)))
        return out


class KeywordReranker:
    model_version = "mock-rerank-1.0"

    def rerank(self, query: str, candidates: list[str]) -> list[float]:
        e = KeywordEmbedding()
        base = e.score(query, candidates)
        return [min(1.0, b + 0.05 * (len(c) > 0)) for b, c in zip(base, candidates)]


class ScriptedLLM:
    """Mock LLM：按脚本返回；default_choice=None表示UNCERTAIN。"""
    prompt_version = "mock-prompt-1.0"

    def __init__(self, default_choice: str | None = None, new_category: bool = False):
        self.default_choice = default_choice
        self.new_category = new_category
        self.calls: list[dict] = []

    def choose(self, evidence: str, options: list[str], allow_uncertain: bool = True) -> dict:
        self.calls.append({"evidence": evidence[:200], "options": options})
        if self.new_category:
            return {"choice": "L9_NEW_MADE_UP", "reason": "scripted-new"}
        if self.default_choice and self.default_choice in options:
            return {"choice": self.default_choice, "reason": "scripted"}
        if allow_uncertain:
            return {"choice": "UNCERTAIN", "reason": "scripted-uncertain"}
        return {"choice": options[0], "reason": "scripted-first"}


class FailingEmbedding(KeywordEmbedding):
    def score(self, query: str, candidates: list[str]) -> list[float]:
        raise RuntimeError("gpu oom (simulated)")


class BgeM3Backend:
    """真实BGE-M3占位：Linux GPU服务器实现，缺依赖时抛明确异常。"""
    model_version = "BAAI/bge-m3"

    def score(self, query: str, candidates: list[str]) -> list[float]:
        try:
            from FlagEmbedding import BGEM3FlagModel  # type: ignore
        except ImportError as e:
            raise RuntimeError(f"bge-m3 not installed on this host: {e}")
        raise RuntimeError("wire to GPU server retriever; not executed on Windows")


class CrossEncoderBackend:
    model_version = "cross-encoder-reranker"

    def rerank(self, query: str, candidates: list[str]) -> list[float]:
        try:
            import sentence_transformers  # type: ignore
        except ImportError as e:
            raise RuntimeError(f"reranker not installed on this host: {e}")
        raise RuntimeError("wire to GPU server reranker; not executed on Windows")


class LLMHttpBackend:
    prompt_version = "llm-judge-v1"

    def __init__(self, endpoint: str = ""):
        self.endpoint = endpoint

    def choose(self, evidence: str, options: list[str], allow_uncertain: bool = True) -> dict:
        raise RuntimeError("wire to LLM endpoint on GPU server; not executed on Windows")
