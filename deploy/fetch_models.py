"""把模型下载到挂载目录，之后 OCR / 分类容器离线运行。可重复执行，已下载的不会重下。

    python deploy/fetch_models.py ocr      # PaddleOCR 检测/方向/识别模型 → ~/.paddleocr
    python deploy/fetch_models.py infer    # bge-m3、bge-reranker-v2-m3 → $HF_HOME
    python deploy/fetch_models.py llm      # 本地大模型兜底 $LLM_MODEL（如 Qwen/Qwen2.5-7B-Instruct，约 15GB）

ops.sh fetch-models 会在对应镜像里各跑一次。国内网络可设 HF_ENDPOINT=https://hf-mirror.com。
"""
from __future__ import annotations

import os
import sys
import time

HF_IGNORE = ["onnx/*", "*.onnx", "*.onnx_data", "imgs/*", "*.h5", "*.msgpack", "*.ot", "*.tflite"]


def fetch_ocr() -> None:
    import numpy as np

    from parser.ocr_paddle import PaddleOcrEngine

    t = time.time()
    engine = PaddleOcrEngine(use_gpu=False)
    engine.warmup()
    lines = engine.recognize(np.full((64, 256, 3), 255, dtype=np.uint8))
    print(f"ocr models ready in {time.time() - t:.0f}s, blank-image lines={len(lines)}")


def fetch_infer() -> None:
    os.environ.pop("HF_HUB_OFFLINE", None)
    os.environ.pop("TRANSFORMERS_OFFLINE", None)
    from huggingface_hub import snapshot_download

    names = [os.environ.get("EMBED_MODEL", "BAAI/bge-m3"), os.environ.get("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")]
    for name in names:
        if os.path.isdir(name):
            print(f"{name} 是本地目录，跳过下载")
            continue
        t = time.time()
        path = snapshot_download(name, ignore_patterns=HF_IGNORE)
        print(f"{name} -> {path} ({time.time() - t:.0f}s)")


def fetch_llm() -> None:
    os.environ.pop("HF_HUB_OFFLINE", None)
    from huggingface_hub import snapshot_download

    name = os.environ.get("LLM_MODEL", "")
    if not name:
        raise SystemExit("LLM_MODEL 没有设置")
    t = time.time()
    path = snapshot_download(name, ignore_patterns=HF_IGNORE + ["original/*", "*.pth", "*.gguf"])
    print(f"{name} -> {path} ({time.time() - t:.0f}s)")


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in ("ocr", "infer", "llm"):
        print(__doc__)
        return 2
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if argv[0] == "ocr":
        fetch_ocr()
    elif argv[0] == "llm":
        fetch_llm()
    else:
        fetch_infer()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
