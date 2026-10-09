from __future__ import annotations
import time
from dataclasses import dataclass, field

STAGES = ["SCAN", "TYPE", "PARSE", "EVIDENCE", "GATE", "L1", "L2", "VERIFY", "SAVE"]
MAX_RETRIES = 3
RETRYABLE = {"RETRYABLE_IO", "OCR_FAILED", "TIMEOUT", "GPU_BUSY"}


@dataclass
class TaskRecord:
    task_id: str
    file_path: str
    stage: str = "SCAN"
    status: str = "PENDING"
    retry_count: int = 0
    error_code: str = ""
    updated_at: float = field(default_factory=time.time)


class Pipeline:
    """按需分流流水线：TYPE→PARSE(文本/OCR/复杂)→分类→SAVE；失败只重跑当前阶段。"""

    def __init__(self, gpu=None, parse_fn=None, classify_fn=None, save_fn=None):
        from scheduler.resources import GPUPool
        self.gpu = gpu or GPUPool()
        self.parse_fn = parse_fn or (lambda fp: {"file_path": fp, "pages": [], "title": ""})
        self.classify_fn = classify_fn or (lambda d: {"result_type": "MANUAL_REVIEW",
                                                      "reason": "no-classifier"})
        self.save_fn = save_fn or (lambda r: None)
        self.records: dict[str, TaskRecord] = {}

    def run_one(self, task_id: str, file_path: str) -> dict:
        rec = self.records.get(task_id) or TaskRecord(task_id, file_path)
        self.records[task_id] = rec
        try:
            parsed = self.parse_fn(file_path)
            rec.stage = "PARSE"
            result = self.classify_fn(parsed)
            rec.stage = "SAVE"
            self.save_fn({**result, "task_id": task_id, "file_path": file_path})
            rec.status = "DONE"
            return {"task_id": task_id, "status": "DONE", "result": result}
        except Exception as e:
            code = getattr(e, "code", "UNKNOWN")
            if code in RETRYABLE and rec.retry_count < MAX_RETRIES:
                rec.retry_count += 1
                rec.status = "RETRY"
                return {"task_id": task_id, "status": "RETRY", "error": str(e)}
            rec.status = "DEAD"
            return {"task_id": task_id, "status": "DEAD", "error": str(e)}

    def resume_pending(self) -> list[str]:
        return [tid for tid, r in self.records.items() if r.status in ("PENDING", "RETRY")]
