"""ISSUE-01：跨Worker交接。PostgreSQL存任务状态；解析中间结果落受控payload文件，
队列只传引用（禁止全文塞Redis）。重启后按stage续跑下一阶段。"""
from __future__ import annotations
import json
import os
import time
from dataclasses import dataclass, asdict

MAX_QUEUE_REF_BYTES = 2000


@dataclass
class HandoffRecord:
    task_id: str
    file_path: str
    stage: str = "SCAN"
    parse_ref: str = ""
    retry_count: int = 0
    updated_at: float = 0.0


class FilePayloadStore:
    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def _payload_path(self, task_id: str) -> str:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in task_id)
        return os.path.join(self.root, f"{safe}.parse.json")

    def _record_path(self, task_id: str) -> str:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in task_id)
        return os.path.join(self.root, f"{safe}.record.json")

    def save(self, task_id: str, stage: str, payload: dict) -> str:
        with open(self._payload_path(task_id), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        ref = f"file://{self._payload_path(task_id)}"
        assert len(ref) < MAX_QUEUE_REF_BYTES, "ref too large for queue"
        self.save_record(HandoffRecord(task_id, payload.get("file_path", ""),
                                       stage, ref, updated_at=time.time()))
        return ref

    def load(self, ref: str) -> dict:
        assert ref.startswith("file://"), f"unsupported ref: {ref[:20]}"
        with open(ref[len("file://"):], encoding="utf-8") as f:
            return json.load(f)

    def save_record(self, rec: HandoffRecord):
        with open(self._record_path(rec.task_id), "w", encoding="utf-8") as f:
            json.dump(asdict(rec), f, ensure_ascii=False)

    def load_record(self, task_id: str) -> HandoffRecord:
        with open(self._record_path(task_id), encoding="utf-8") as f:
            d = json.load(f)
        return HandoffRecord(**d)

    def pending_from(self, stage: str) -> list[HandoffRecord]:
        out = []
        for fn in os.listdir(self.root):
            if fn.endswith(".record.json"):
                with open(os.path.join(self.root, fn), encoding="utf-8") as f:
                    rec = HandoffRecord(**json.load(f))
                if rec.stage == stage:
                    out.append(rec)
        return out
