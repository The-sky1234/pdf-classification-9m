from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from collections import OrderedDict


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

    def choose(self, evidence: str, options: list[str], allow_uncertain: bool = True,
               descriptions: dict[str, str] | None = None) -> dict:
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


def _device(requested: str | None) -> str:
    if requested:
        return requested
    try:
        import torch  # type: ignore
    except ImportError:
        return "cpu"
    return "cuda" if torch.cuda.is_available() else "cpu"


class BgeM3Backend:
    """BAAI/bge-m3 稠密向量，余弦相似度。候选说明只编码一次；同一段证据连着问一级、二级时也只编码一次。"""

    def __init__(self, model_name: str | None = None, device: str | None = None,
                 batch_size: int = 32, max_length: int = 1024, fp16: bool = True):
        self.model_name = model_name or os.environ.get("EMBED_MODEL", "BAAI/bge-m3")
        self.model_version = self.model_name
        self.device = _device(device or os.environ.get("MODEL_DEVICE"))
        self.batch_size = batch_size
        self.max_length = max_length
        self.fp16 = fp16 and self.device.startswith("cuda")
        self._model = None
        self._lock = threading.Lock()
        self._cand: dict[str, object] = {}
        self._queries: OrderedDict[str, object] = OrderedDict()
        self._query_cap = 8

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer  # type: ignore

            model = SentenceTransformer(self.model_name, device=self.device)
            model.max_seq_length = self.max_length
            if self.fp16:
                model.half()
            self._model = model
        return self._model

    def _encode(self, texts: list[str]):
        return self._load().encode(texts, batch_size=self.batch_size, normalize_embeddings=True,
                                   convert_to_numpy=True, show_progress_bar=False)

    def warmup(self) -> None:
        self._encode(["预热"])

    def prime(self, queries: list[str]) -> None:
        """一批证据一次编码，后面逐个 score 时直接命中缓存。"""
        with self._lock:
            todo = [q for q in dict.fromkeys(queries) if q not in self._queries]
            self._query_cap = max(8, len(queries) * 2)
            if todo:
                for text, vec in zip(todo, self._encode(todo)):
                    self._queries[text] = vec
            while len(self._queries) > self._query_cap:
                self._queries.popitem(last=False)

    def score(self, query: str, candidates: list[str]) -> list[float]:
        with self._lock:
            missing = [c for c in dict.fromkeys(candidates) if c not in self._cand]
            if missing:
                for text, vec in zip(missing, self._encode(missing)):
                    self._cand[text] = vec
            q = self._queries.get(query)
            if q is None:
                q = self._encode([query])[0]
                self._queries[query] = q
                if len(self._queries) > self._query_cap:
                    self._queries.popitem(last=False)
            return [float((self._cand[c] * q).sum()) for c in candidates]


class CrossEncoderBackend:
    """BAAI/bge-reranker-v2-m3。输出经 sigmoid 落在 0-1。"""

    def __init__(self, model_name: str | None = None, device: str | None = None,
                 batch_size: int = 16, max_length: int = 1024, fp16: bool = True):
        self.model_name = model_name or os.environ.get("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
        self.model_version = self.model_name
        self.device = _device(device or os.environ.get("MODEL_DEVICE"))
        self.batch_size = batch_size
        self.max_length = max_length
        self.fp16 = fp16 and self.device.startswith("cuda")
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        if self._model is None:
            from sentence_transformers import CrossEncoder  # type: ignore

            model = CrossEncoder(self.model_name, device=self.device, max_length=self.max_length)
            if self.fp16:
                model.model.half()
            self._model = model
        return self._model

    def warmup(self) -> None:
        self.rerank("预热", ["预热"])

    def rerank(self, query: str, candidates: list[str]) -> list[float]:
        import torch  # type: ignore

        with self._lock:
            scores = self._load().predict([(query, c) for c in candidates], batch_size=self.batch_size,
                                          activation_fct=torch.nn.Sigmoid(), show_progress_bar=False)
        return [float(v) for v in scores]


class LLMHttpBackend:
    """OpenAI 兼容的 /chat/completions（vLLM、Xinference、Ollama 等都行）。

    只许在给定候选里选，或回 UNCERTAIN。超过 max_calls 次以后一律 UNCERTAIN，
    由上游转人工复核，不会因为大模型不可用而卡住整个波次。
    """

    prompt_version = "llm-judge-v3"

    def __init__(self, endpoint: str = "", api_key: str = "", model: str = "",
                 timeout_s: float = 30.0, max_calls: int | None = None):
        self.endpoint = (endpoint or os.environ.get("LLM_ENDPOINT", "")).rstrip("/")
        self.api_key = api_key or os.environ.get("LLM_API_KEY", "")
        self.model = model or os.environ.get("LLM_MODEL", "")
        self.timeout_s = timeout_s
        limit = max_calls if max_calls is not None else os.environ.get("LLM_MAX_CALLS", "0")
        self.max_calls = int(limit)
        self.calls = 0
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.endpoint and self.model and self.max_calls > 0)

    def _budget(self) -> bool:
        with self._lock:
            if self.calls >= self.max_calls:
                return False
            self.calls += 1
            return True

    def choose(self, evidence: str, options: list[str], allow_uncertain: bool = True,
               descriptions: dict[str, str] | None = None) -> dict:
        if not self.enabled or not self._budget():
            return {"choice": "UNCERTAIN", "reason": "llm_disabled_or_budget_exhausted"}
        lines = [f"{opt}：{(descriptions or {}).get(opt, '')}".rstrip("：") for opt in options]
        prompt = (
            "你是档案分类员。根据文件证据，从候选里选一个最合适的，只回答候选前面的编号原文。"
            + ("拿不准就回答 UNCERTAIN。" if allow_uncertain else "")
            + "\n候选：\n" + "\n".join(lines) + "\n\n文件证据：\n" + evidence[:3000]
        )
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": 32,
        }).encode("utf-8")
        req = urllib.request.Request(self.endpoint + "/chat/completions", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            answer = data["choices"][0]["message"]["content"].strip()
        except (urllib.error.URLError, TimeoutError, KeyError, IndexError, ValueError) as exc:
            raise RuntimeError(f"llm call failed: {exc}") from exc
        for opt in sorted(options, key=len, reverse=True):
            if answer == opt or answer.startswith(opt):
                return {"choice": opt, "reason": "llm"}
        if "UNCERTAIN" in answer.upper():
            return {"choice": "UNCERTAIN", "reason": "llm_uncertain"}
        return {"choice": answer, "reason": "llm_off_list"}
