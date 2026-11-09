from __future__ import annotations

import json
import os
import re
import threading
import time
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

    只许在给定候选里选，或回 UNCERTAIN。超过 max_calls 次（每个进程从启动起计）以后一律 UNCERTAIN，
    由上游转人工复核。重试用完仍连不上时熔断 cooldown_s 秒，期间直接报错转人工，不拖慢整个波次。
    """

    prompt_version = "llm-judge-v4"

    def __init__(self, endpoint: str = "", api_key: str = "", model: str = "",
                 timeout_s: float | None = None, max_calls: int | None = None, max_tokens: int | None = None,
                 retries: int = 3, retry_wait_s: float = 2.0, cooldown_s: float | None = None):
        self.endpoint = (endpoint or os.environ.get("LLM_ENDPOINT", "")).rstrip("/")
        self.api_key = api_key or os.environ.get("LLM_API_KEY", "")
        self.model = model or os.environ.get("LLM_MODEL", "")
        self.timeout_s = timeout_s if timeout_s is not None else float(os.environ.get("LLM_TIMEOUT_S", "60"))
        limit = max_calls if max_calls is not None else os.environ.get("LLM_MAX_CALLS", "0")
        self.max_calls = int(limit)
        self.max_tokens = max_tokens if max_tokens is not None else int(os.environ.get("LLM_MAX_TOKENS", "32"))
        self.retries = retries
        self.retry_wait_s = retry_wait_s
        self.cooldown_s = cooldown_s if cooldown_s is not None else float(os.environ.get("LLM_COOLDOWN_S", "300"))
        self.calls = 0
        self._down_until = 0.0
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

    def _refund(self) -> None:
        with self._lock:
            self.calls -= 1

    def choose(self, evidence: str, options: list[str], allow_uncertain: bool = True,
               descriptions: dict[str, str] | None = None) -> dict:
        if not self.enabled:
            return {"choice": "UNCERTAIN", "reason": "llm_disabled_or_budget_exhausted"}
        if time.monotonic() < self._down_until:
            raise RuntimeError("llm unavailable (cooling down)")
        if not self._budget():
            return {"choice": "UNCERTAIN", "reason": "llm_disabled_or_budget_exhausted"}
        prompt = build_prompt(evidence, options, descriptions or {}, allow_uncertain)
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": self.max_tokens,
        }).encode("utf-8")
        try:
            raw = self._post(body)
        except RuntimeError:
            self._refund()
            raise
        answer = _strip_thinking(raw)
        if answer is None:
            return {"choice": "UNCERTAIN", "reason": "llm_thinking_truncated"}
        return parse_answer(answer, options, allow_uncertain)

    def _post(self, body: bytes) -> str:
        """连不上、超时、5xx 重试几次（服务刚启动或重启时）；4xx 和返回格式不对不重试。"""
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(self.endpoint + "/chat/completions", data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
            if self.api_key:
                req.add_header("Authorization", f"Bearer {self.api_key}")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                return (data["choices"][0]["message"]["content"] or "").strip()
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code < 500:
                    break
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last = exc
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise RuntimeError(f"llm call failed: bad response: {exc}") from exc
            if attempt < self.retries:
                time.sleep(self.retry_wait_s * (2 ** attempt))
        if not isinstance(last, urllib.error.HTTPError) or last.code >= 500:
            self._down_until = time.monotonic() + self.cooldown_s
        raise RuntimeError(f"llm call failed: {last}") from last


def build_prompt(evidence: str, options: list[str], descriptions: dict[str, str], allow_uncertain: bool) -> str:
    """候选编号 1..N，只让模型回一个数字：7B 级别的模型回类目名时常改写或答成说明里的三级词。"""
    lines = [f"{i}. {descriptions.get(opt) or opt}" for i, opt in enumerate(options, 1)]
    tail = f"只输出一个数字（1-{len(options)}）" + ("；都不合适或拿不准就输出 0。" if allow_uncertain else "，必须选一个。")
    return ("你是应急管理部门的档案分类员。阅读下面的文件内容，判断它最属于哪一个类别。\n类别：\n"
            + "\n".join(lines) + "\n\n文件内容：\n" + evidence[:3000] + "\n\n" + tail)


def parse_answer(answer: str, options: list[str], allow_uncertain: bool) -> dict:
    for opt in sorted(options, key=len, reverse=True):
        if answer == opt or answer.startswith(opt):
            return {"choice": opt, "reason": "llm"}
    match = re.search(r"(?<![A-Za-z0-9_.])(\d+)(?![A-Za-z0-9_])", answer) if len(answer) <= 16 else None
    if match:
        n = int(match.group(1))
        if 1 <= n <= len(options):
            return {"choice": options[n - 1], "reason": "llm"}
        if n == 0 and allow_uncertain:
            return {"choice": "UNCERTAIN", "reason": "llm_uncertain"}
    if "UNCERTAIN" in answer.upper() or "拿不准" in answer:
        return {"choice": "UNCERTAIN", "reason": "llm_uncertain"}
    return {"choice": answer, "reason": "llm_off_list"}


def _strip_thinking(answer: str) -> str | None:
    """Qwen3、DeepSeek-R1 这类模型先输出 <think>…</think>。思考没写完就被截断时返回 None。"""
    if "<think>" not in answer:
        return answer
    if "</think>" not in answer:
        return None
    return answer.rsplit("</think>", 1)[1].strip()
