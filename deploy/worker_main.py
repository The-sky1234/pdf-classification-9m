"""Worker消费进程（deploy层装配，不碰业务逻辑）。
ISSUE-01：parser/ocr/mineru角色解析后经FilePayloadStore持久化并转发classify交接
（队列只传引用）；classify角色消费引用→分类→HTTP回写。
ISSUE-02：回写需API_KEY/API_URL，缺失启动即失败，不关闭认证不硬编码。
ISSUE-09：模型调用点持FileGpuLease（跨进程文件锁）。
"""
from __future__ import annotations
import json
import os
import signal
import sys
import time

ROLE = os.environ.get("WORKER_ROLE", "parser")
REDIS_URL = os.environ.get("REDIS_URL", "")
PDF_ROOT = os.environ.get("PDF_ROOT", "/data/pdfs")
PAYLOAD_DIR = os.environ.get("PAYLOAD_DIR", "/data/payloads")
LOCK_DIR = os.environ.get("LOCK_DIR", "/data/locks")
_stop = False


def _handle(signum, frame):
    global _stop
    _stop = True


signal.signal(signal.SIGTERM, _handle)
signal.signal(signal.SIGINT, _handle)


def require_env(name: str) -> str:
    val = os.environ.get(name, "")
    if not val:
        raise SystemExit(f"missing required env {name}")
    return val


def build_handlers(role: str):
    from scheduler.queue import RedisQueue
    from scheduler.handoff import FilePayloadStore
    from scheduler.gpu_lock import FileGpuLease
    from scheduler.pipeline import Pipeline
    q = RedisQueue(REDIS_URL or "redis://redis:6379/0")
    store = FilePayloadStore(PAYLOAD_DIR)
    if role in ("parser", "ocr", "mineru"):
        from parser.documents import parse_for_role
        lease_group = {"parser": None, "ocr": "ocr", "mineru": "mineru"}[role]
        mineru = None
        if role == "mineru":
            from parser.mineru_adapter import MinerUAdapter
            mineru = MinerUAdapter()

        def parse_and_forward(fp: str) -> dict:
            if lease_group:
                with FileGpuLease(LOCK_DIR, lease_group):
                    return parse_for_role(fp, role, mineru=mineru)
            return parse_for_role(fp, role)

        def forward_only(_):
            return {"result_type": "MANUAL_REVIEW",
                    "reason": "forwarded-to-classify"}

        pipe = Pipeline(parse_fn=parse_and_forward, classify_fn=forward_only,
                        save_fn=lambda r: None)
        return q, pipe, store
    if role == "classify":
        api_url, api_key = require_env("API_URL"), require_env("API_KEY")
        from classifier.pipeline import ClassificationPipeline
        from classifier.config import Thresholds
        from classifier.backends import BgeM3Backend, CrossEncoderBackend, LLMHttpBackend
        clf = ClassificationPipeline(BgeM3Backend(), CrossEncoderBackend(),
                                     LLMHttpBackend(os.environ.get("LLM_ENDPOINT", "")),
                                     Thresholds(calibrated=os.environ.get(
                                         "THRESHOLDS_CALIBRATED", "0") == "1"))

        def classify_ref(payload: dict) -> dict:
            ref = payload.get("parse_ref", "")
            parsed = store.load(ref) if ref else payload
            with FileGpuLease(LOCK_DIR, "infer"):
                return clf.run(parsed)

        def save_result(res: dict):
            import urllib.request
            req = urllib.request.Request(
                api_url.rstrip("/") + "/results",
                data=json.dumps(res).encode(),
                headers={"Content-Type": "application/json", "X-API-Key": api_key})
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status != 201:
                    raise RuntimeError(f"backend rejected: {resp.status}")

        pipe = Pipeline(parse_fn=lambda fp: {"file_path": fp},
                        classify_fn=classify_ref, save_fn=save_result)
        return q, pipe, store
    raise SystemExit(f"unknown WORKER_ROLE={role}")


def main() -> int:
    if ROLE in ("decide", "archive"):
        from scheduler.phase1_loop import serve
        return serve(ROLE, lambda: _stop)
    q, pipe, store = build_handlers(ROLE)
    idle = 0
    while not _stop:
        item = q.dequeue()
        if not item:
            idle += 1
            time.sleep(min(5.0, 0.1 * idle))
            continue
        idle = 0
        task_id, payload = item
        try:
            if ROLE in ("parser", "ocr", "mineru"):
                parsed = pipe.parse_fn(payload.get("file_path", ""))
                if not parsed.get("ok", True):
                    q.to_dead({"task_id": task_id, "error": parsed.get("error")})
                    continue
                ref = store.save(task_id, "PARSED", parsed)
                fwd = {"task_id": task_id, "file_path": payload.get("file_path", ""),
                       "stage": "PARSED", "parse_ref": ref}
                q.enqueue(task_id + ":classify", fwd)
                print(json.dumps({"worker": ROLE, "task": task_id, "status": "PARSED"}))
                continue
            res = pipe.classify_fn(payload)
            pipe.save_fn({**res, "task_id": task_id,
                          "file_path": payload.get("file_path", "")})
            print(json.dumps({"worker": ROLE, "task": task_id,
                              "status": res.get("result_type")}))
            continue
        except Exception as e:
            print(json.dumps({"worker": ROLE, "task": task_id, "fatal": str(e)}),
                  file=sys.stderr)
            q.to_dead({"task_id": task_id, "error": str(e)})
            continue
    print(json.dumps({"worker": ROLE, "event": "graceful-stop"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
