"""波次执行器。每个进程循环：领一批 → 逐个处理 → 写回台账。

    cpu      波次 0：抽文字、规则。定了的进 archive，扫描件进 ocr，其余进 classify
    ocr      波次 1：PaddleOCR 识别扫描页和图片，再走一遍规则
    classify 波次 2：bge-m3 + reranker 选场景；阈值未校准一律待复核
    archive  按结论复制到归档根，核对 SHA-256；复核改判的同时删掉旧副本

同一时间只跑一个 GPU 波次。GPU 波次的主进程持有 scheduler.gpu_lock 的跨进程锁，
不同波次的容器误开了也只会排队，不会同时上卡。
"""
from __future__ import annotations

import json
import logging
import multiprocessing as mp
import os
import signal
import socket
import time

from phase1 import ledger
from phase1.extract import Extract, extract_cpu
from phase1.judge import classify_query, judge_file

log = logging.getLogger("phase1.waves")

GPU_STAGES = ("ocr", "classify")
DEFAULTS = {
    #          batch, lease 秒, 单文件超时秒
    "cpu": (50, 900, 120),
    "archive": (20, 1800, 1200),
    "ocr": (4, 1800, 600),
    "classify": (64, 900, 120),
}


class FileTimeout(Exception):
    pass


class Handler:
    """每个波次一个。setup 在子进程里调用，模型在这里加载。"""

    stage = ""

    def setup(self) -> None:
        pass

    def before_batch(self, rows: list[dict]) -> None:
        pass

    def process(self, row: dict) -> dict:
        raise NotImplementedError


class CpuHandler(Handler):
    stage = "cpu"

    def process(self, row: dict) -> dict:
        path = row["source_path"]
        got = extract_cpu(path)
        if got.outcome == "retry":
            raise RetryLater(f"{got.error_code}: {got.message}")
        decision = judge_file(path, extracted=got)
        if decision.settled:
            values = ledger.decision_values(decision)
            values["snippet"] = got.text[: ledger.SNIPPET_CHARS] or None
            return values
        values = {
            "stage": decision.next_wave,
            "reason": decision.reason,
            "page_count": got.page_count or None,
            "evidence": ledger.pack_text(got.text) if got.text else None,
            "snippet": got.text[: ledger.SNIPPET_CHARS] or None,
            "ocr_pages": json.dumps(got.ocr_pages) if got.ocr_pages else None,
            "attempts": 0,
            "last_error": None,
        }
        return values


class OcrHandler(Handler):
    stage = "ocr"

    def __init__(self, engine_factory=None):
        self.engine_factory = engine_factory
        self.engine = None

    def setup(self) -> None:
        if self.engine_factory is None:
            from parser.ocr_paddle import PaddleOcrEngine

            self.engine_factory = PaddleOcrEngine
        self.engine = self.engine_factory()
        if hasattr(self.engine, "warmup"):
            self.engine.warmup()

    def process(self, row: dict) -> dict:
        from phase1.ocr import OcrUnreadable, ocr_file

        path = row["source_path"]
        if not os.path.isfile(path):
            raise RetryLater(f"missing: {path}")
        pages = json.loads(row["ocr_pages"]) if row.get("ocr_pages") else []
        try:
            got = ocr_file(path, ledger.unpack_text(row.get("evidence")), pages, self.engine)
        except OcrUnreadable as exc:
            got = Extract(path, row.get("extension") or "", outcome="needs_review", error_code="OCR_UNREADABLE",
                          message=str(exc))
        decision = judge_file(path, extracted=got)
        if decision.settled:
            values = ledger.decision_values(decision)
            values["snippet"] = got.text[: ledger.SNIPPET_CHARS] or row.get("snippet")
            values["last_error"] = None
            return values
        return {
            "stage": "classify",
            "reason": "awaiting_classify",
            "evidence": ledger.pack_text(got.text),
            "snippet": got.text[: ledger.SNIPPET_CHARS] or None,
            "ocr_pages": None,
            "attempts": 0,
            "last_error": None,
        }


class ClassifyHandler(Handler):
    stage = "classify"

    def __init__(self, classifier_factory=None):
        self.classifier_factory = classifier_factory
        self.classify = None

    def setup(self) -> None:
        self.classify = (self.classifier_factory or build_scene_classifier)()

    def before_batch(self, rows: list[dict]) -> None:
        prime = getattr(self.classify, "prime", None)
        if prime:
            prime([classify_query(r["source_path"], ledger.unpack_text(r.get("evidence"))) for r in rows])

    def process(self, row: dict) -> dict:
        path = row["source_path"]
        text = ledger.unpack_text(row.get("evidence"))
        got = Extract(path, row.get("extension") or "", text=text, outcome="text",
                      page_count=row.get("page_count") or 0)
        decision = judge_file(path, classify_fn=self.classify, extracted=got)
        if not decision.settled:
            raise RuntimeError(f"分类后仍未定论：{decision.reason}")
        return ledger.decision_values(decision)


class ArchiveHandler(Handler):
    stage = "archive"

    def __init__(self, archive_root: str):
        if not archive_root:
            raise SystemExit("ARCHIVE_ROOT 没有配置，archive 波次不能启动")
        self.archive_root = archive_root

    def setup(self) -> None:
        os.makedirs(self.archive_root, exist_ok=True)
        probe = os.path.join(self.archive_root, f".write-test-{os.getpid()}")
        with open(probe, "w") as handle:
            handle.write("ok")
        os.remove(probe)

    def process(self, row: dict) -> dict:
        from phase1.archive import copy_into, remove_copy

        src = row["source_path"]
        if not os.path.isfile(src):
            raise RetryLater(f"missing: {src}")
        out = copy_into(src, self.archive_root, row["target_dir"])
        old = row.get("previous_archive_path")
        if old and os.path.realpath(old) != os.path.realpath(out["final"]):
            remove_copy(old, self.archive_root, row.get("previous_archive_sha256") or "")
        return {
            "stage": "done",
            "archive_path": out["final"],
            "archive_sha256": out["sha256"],
            "archive_bytes": out["nbytes"],
            "previous_archive_path": None,
            "previous_archive_sha256": None,
            "attempts": 0,
            "last_error": None,
        }


class RetryLater(Exception):
    pass


def build_scene_classifier():
    from classifier.backends import BgeM3Backend, CrossEncoderBackend, LLMHttpBackend
    from classifier.config import Thresholds
    from phase1.scenes import make_scene_classifier

    th = Thresholds.load()
    emb = BgeM3Backend()
    reranker = CrossEncoderBackend()
    emb.warmup()
    reranker.warmup()
    classify = make_scene_classifier(emb, reranker, LLMHttpBackend(), th)
    classify.prime = lambda texts: emb.prime([t[: th.max_evidence_chars] for t in texts])
    log.info("classify ready: emb=%s rerank=%s calibrated=%s", emb.model_name, reranker.model_name, th.calibrated)
    return classify


def make_handler(stage: str, **kw) -> Handler:
    if stage == "cpu":
        return CpuHandler()
    if stage == "ocr":
        return OcrHandler(kw.get("ocr_factory"))
    if stage == "classify":
        return ClassifyHandler(kw.get("classifier_factory"))
    if stage == "archive":
        return ArchiveHandler(kw.get("archive_root") or os.environ.get("ARCHIVE_ROOT", ""))
    raise ValueError(stage)


class _Stop:
    flag = False


def _on_term(_sig, _frm):
    _Stop.flag = True


def _alarm(_sig, _frm):
    raise FileTimeout("单文件处理超时")


def sources_ready(roots: list[str]) -> bool:
    """源盘没挂上时所有文件都会「不存在」。先看根目录在不在、是不是空的，别把整批打成失败。"""
    for root in roots:
        try:
            with os.scandir(root) as it:
                next(it)
        except (OSError, StopIteration):
            return False
    return True


def run_loop(db_url: str, stages: list[str], *, batch: int | None = None, lease_s: float | None = None,
             timeout_s: int | None = None, max_attempts: int = 3, until_empty: bool = False,
             idle_sleep: float = 10.0, backoff_s: float = 60.0, handler_kw: dict | None = None) -> dict:
    """一个进程的工作循环。返回处理计数。"""
    engine = ledger.make_engine(db_url)
    handlers = {s: make_handler(s, **(handler_kw or {})) for s in stages}
    for handler in handlers.values():
        handler.setup()
    owner = f"{socket.gethostname()}:{os.getpid()}"[:80]
    roots = [r for r in os.environ.get("SOURCE_ROOTS", "").split(os.pathsep) if r]
    counts = {"done": 0, "retry": 0, "failed": 0, "lost_lease": 0}
    use_alarm = hasattr(signal, "SIGALRM")
    if use_alarm:
        signal.signal(signal.SIGALRM, _alarm)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _on_term)
    while not _Stop.flag:
        if roots and not sources_ready(roots):
            log.warning("源目录不可读：%s，%ss 后再看", roots, idle_sleep)
            time.sleep(idle_sleep)
            continue
        worked = False
        for stage, handler in handlers.items():
            size, lease, limit = DEFAULTS[stage]
            rows = ledger.claim(engine, stage, owner, batch or size, lease_s or lease, max_attempts)
            if not rows:
                continue
            worked = True
            handler.before_batch(rows)
            results: list[tuple[int, dict]] = []
            unprocessed: list[int] = []
            for row in rows:
                if _Stop.flag:
                    unprocessed.append(row["id"])
                    continue
                try:
                    if use_alarm:
                        signal.alarm(int(timeout_s or limit))
                    values = handler.process(row)
                    counts["done"] += 1
                except (RetryLater, FileTimeout, OSError) as exc:
                    values = ledger.retry_later(row, f"{type(exc).__name__}: {exc}", max_attempts, backoff_s)
                    counts["failed" if values["stage"] == "failed" else "retry"] += 1
                except Exception as exc:  # noqa: BLE001 单个文件的任何异常都不能拖垮整批
                    log.exception("处理失败 %s", row["source_path"])
                    values = ledger.retry_later(row, f"{type(exc).__name__}: {exc}", max_attempts, backoff_s)
                    counts["failed" if values["stage"] == "failed" else "retry"] += 1
                finally:
                    if use_alarm:
                        signal.alarm(0)
                results.append((row["id"], values))
            written = ledger.finish(engine, owner, results)
            counts["lost_lease"] += len(results) - written
            if unprocessed:
                ledger.release(engine, owner, unprocessed)
            log.info("%s: %d 行写回（累计 %s）", stage, written, counts)
        if not worked:
            if until_empty:
                break
            time.sleep(idle_sleep)
    engine.dispose()
    return counts


def _child(db_url: str, stages: list[str], opts: dict) -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(process)d %(levelname)s %(message)s")
    run_loop(db_url, stages, **opts)


def run_workers(db_url: str, stages: list[str], procs: int = 1, *, gpu_lock_dir: str | None = None,
                **opts) -> int:
    """起 procs 个工作进程。含 GPU 波次时，先拿到显卡锁再起子进程，退出时释放。"""
    ledger.ensure_schema(ledger.make_engine(db_url))
    lease = None
    if any(s in GPU_STAGES for s in stages):
        from scheduler.gpu_lock import GPU_LEASE_GROUP, FileGpuLease

        lease = FileGpuLease(gpu_lock_dir or os.environ.get("LOCK_DIR", "/data/locks"), GPU_LEASE_GROUP)
        while not lease.acquire(timeout=60):
            log.warning("显卡被别的波次占着（%s），继续等", lease.holder() or "?")
        log.info("拿到显卡锁 %s", lease.path)
    try:
        if procs <= 1:
            _child(db_url, stages, opts)
            return 0
        ctx = mp.get_context("spawn")
        children = [ctx.Process(target=_child, args=(db_url, stages, opts), daemon=False) for _ in range(procs)]
        for child in children:
            child.start()

        def _forward(sig, _frm):
            for child in children:
                if child.is_alive():
                    os.kill(child.pid, sig)

        signal.signal(signal.SIGTERM, _forward)
        signal.signal(signal.SIGINT, _forward)
        for child in children:
            child.join()
        return max((c.exitcode or 0) for c in children)
    finally:
        if lease is not None:
            lease.release()
