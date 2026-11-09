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

    @staticmethod
    def _atomic_write(path: str, data) -> None:
        """先写同目录临时文件再改名，进程中途被杀也不会留下半个 JSON。"""
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def save(self, task_id: str, stage: str, payload: dict) -> str:
        self._atomic_write(self._payload_path(task_id), payload)
        ref = f"file://{self._payload_path(task_id)}"
        if len(ref) >= MAX_QUEUE_REF_BYTES:
            raise ValueError("ref too large for queue")
        self.save_record(HandoffRecord(task_id, payload.get("file_path", ""),
                                       stage, ref, updated_at=time.time()))
        return ref

    def load(self, ref: str) -> dict:
        if not ref.startswith("file://"):
            raise ValueError(f"unsupported ref: {ref[:20]}")
        with open(ref[len("file://"):], encoding="utf-8") as f:
            return json.load(f)

    def save_record(self, rec: HandoffRecord):
        self._atomic_write(self._record_path(rec.task_id), asdict(rec))

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
