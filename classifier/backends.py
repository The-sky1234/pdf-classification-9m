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
    """BGE-M3 稠密向量。用查询与类别说明的余弦相似度打分。"""

    def __init__(self, model_name: str | None = None):
        import os
        self.model_version = model_name or os.environ.get("BGE_MODEL", "BAAI/bge-m3")
        self._model = None

    def ensure_available(self) -> None:
        try:
            from FlagEmbedding import BGEM3FlagModel  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                f"分类要加载 {self.model_version}，请先安装 FlagEmbedding：{exc}"
            ) from exc

    def score(self, query: str, candidates: list[str]) -> list[float]:
        if not candidates:
            return []
        self.ensure_available()
        model = self._load()
        query_vec = _dense_rows(model.encode([query], batch_size=1, max_length=4096))[0]
        rows = _dense_rows(model.encode(list(candidates), batch_size=8, max_length=512))
        if len(rows) != len(candidates):
            raise RuntimeError("bge-m3 returned a different number of vectors")
        return [_cosine(query_vec, row) for row in rows]

    def _load(self):
        if self._model is not None:
            return self._model
        import os
        from FlagEmbedding import BGEM3FlagModel
        use_fp16 = os.environ.get("BGE_FP16", "1") == "1"
        device = os.environ.get("BGE_DEVICE", "").strip()
        kwargs = {"use_fp16": use_fp16}
        if device:
            kwargs["devices"] = device
        try:
            self._model = BGEM3FlagModel(self.model_version, **kwargs)
        except TypeError:
            kwargs.pop("devices", None)
            self._model = BGEM3FlagModel(self.model_version, use_fp16=use_fp16)
        return self._model


class CrossEncoderBackend:
    """BGE Reranker。二级候选精排，输出 0 到 1 的分数。"""

    def __init__(self, model_name: str | None = None):
        import os
        self.model_version = model_name or os.environ.get(
            "RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
        self._model = None

    def ensure_available(self) -> None:
        try:
            from sentence_transformers import CrossEncoder  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                f"分类要加载 {self.model_version}，请先安装 sentence-transformers：{exc}"
            ) from exc

    def rerank(self, query: str, candidates: list[str]) -> list[float]:
        if not candidates:
            return []
        self.ensure_available()
        model = self._load()
        raw = model.predict([(query, item) for item in candidates], show_progress_bar=False)
        values = [float(item) for item in list(raw)]
        if any(item < 0.0 or item > 1.0 for item in values):
            values = [_sigmoid(item) for item in values]
        return values

    def _load(self):
        if self._model is not None:
            return self._model
        import os
        from sentence_transformers import CrossEncoder
        device = os.environ.get("RERANKER_DEVICE", os.environ.get("BGE_DEVICE", "")).strip()
        self._model = CrossEncoder(self.model_version, device=device or None)
        return self._model


class LLMHttpBackend:
    """疑难件裁决。只接受 OpenAI 兼容的 /v1/chat/completions，不在进程里放权重。"""

    prompt_version = "llm-judge-v1"

    def __init__(self, endpoint: str = ""):
        self.endpoint = endpoint

    def choose(self, evidence: str, options: list[str], allow_uncertain: bool = True) -> dict:
        import json
        import os
        import urllib.request
        endpoint = (self.endpoint or os.environ.get("LLM_ENDPOINT", "")).strip().rstrip("/")
        if not endpoint:
            raise RuntimeError("LLM_ENDPOINT is empty")
        url = endpoint if endpoint.endswith("/chat/completions") else endpoint + "/chat/completions"
        allowed = list(options)
        if allow_uncertain and "UNCERTAIN" not in allowed:
            allowed = allowed + ["UNCERTAIN"]
        model = os.environ.get("LLM_MODEL", "Qwen2.5-7B-Instruct")
        body = {
            "model": model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": (
                    "你是文档分类员。只能从给定选项中选一个；证据不足必须选 UNCERTAIN。"
                    "禁止发明新类别。只输出 JSON："
                    "{\"choice\":\"选项原文\",\"reason\":\"一句依据\"}"
                )},
                {"role": "user", "content": f"选项：{allowed}\n\n证据：\n{evidence[:6000]}"},
            ],
        }
        headers = {"Content-Type": "application/json"}
        key = os.environ.get("LLM_API_KEY", "").strip()
        if key and not key.startswith("CHANGE_ME"):
            headers["Authorization"] = f"Bearer {key}"
        timeout = float(os.environ.get("LLM_TIMEOUT", "30"))
        request = urllib.request.Request(
            url, data=json.dumps(body).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        content = payload["choices"][0]["message"]["content"]
        return _parse_model_json(content)


def _dense_rows(encoded) -> list[list[float]]:
    if isinstance(encoded, dict):
        encoded = encoded.get("dense_vecs")
    if hasattr(encoded, "tolist"):
        encoded = encoded.tolist()
    if encoded and isinstance(encoded[0], (int, float)):
        return [[float(item) for item in encoded]]
    return [[float(item) for item in row] for row in encoded]


def _cosine(left: list[float], right: list[float]) -> float:
    import math
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left)) or 1e-9
    right_norm = math.sqrt(sum(b * b for b in right)) or 1e-9
    return dot / (left_norm * right_norm)


def _sigmoid(value: float) -> float:
    import math
    if value >= 60:
        return 1.0
    if value <= -60:
        return 0.0
    return 1.0 / (1.0 + math.exp(-value))


def _parse_model_json(content: str) -> dict:
    import json
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        data = json.loads(text[start:end + 1])
        return {"choice": str(data.get("choice") or "").strip(),
                "reason": str(data.get("reason") or "")[:500]}
    return {"choice": text.strip().strip('"'), "reason": ""}
