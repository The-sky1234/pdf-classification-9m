from __future__ import annotations
import os
from pathlib import Path
from fastapi import FastAPI, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from backend.models import make_engine, make_session, init_db
from backend import store
from backend.schemas import ScanRequest, ReviewPatch
from backend.auth import require_key
from registry.register import scan_register
from scheduler.queue import open_queue


def _load_local_env() -> None:
    """读取仓库根目录未跟踪的 .env。已存在的环境变量优先，不记录变量值。"""
    path = Path(__file__).resolve().parents[1] / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key.isascii() or not key.replace("_", "").isalnum() or key in os.environ:
            continue
        os.environ[key] = value.strip().strip('"').strip("'")


_load_local_env()
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./dev.db")


def build_app(db_url: str = DATABASE_URL) -> FastAPI:
    engine = make_engine(db_url)
    init_db(engine)
    sessions = make_session(engine)
    app = FastAPI(title="900万PDF智能分类系统API", version="v3.0-phase1")
    app.state.sessions = sessions
    app.state.pdf_queue = open_queue("pdf_tasks")
    app.state.archive_queue = open_queue("archive_tasks")

    def db():
        s = sessions()
        try:
            yield s
        finally:
            s.close()

    @app.post("/tasks/scan", status_code=202, dependencies=[Depends(require_key)])
    def scan(req: ScanRequest, s: Session = Depends(db)):
        if not os.path.isdir(req.root):
            raise HTTPException(400, "root is not a directory")
        try:
            out = scan_register(s, req.root, req.limit, req.batch_id, os.environ.get("ARCHIVE_ROOT"))
        except ValueError as e:
            msg = str(e)
            if msg == "batch not found":
                raise HTTPException(404, msg)
            raise HTTPException(400, msg)
        for item in out["items"]:
            app.state.pdf_queue.enqueue(item["task_id"], {
                "v": 1, "file_id": item["file_id"], "task_id": item["task_id"],
                "content_version": item["content_version"], "source_path": item["source_path"]})
        return out

    @app.get("/tasks/{task_id}", dependencies=[Depends(require_key)])
    def task_status(task_id: str, s: Session = Depends(db)):
        view = store.task_view(s, task_id)
        if not view:
            raise HTTPException(404, "task not found")
        return view

    @app.get("/results", dependencies=[Depends(require_key)])
    def results(result_type: str | None = None, level1: str | None = None,
                archive_status: str | None = None,
                page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=200),
                s: Session = Depends(db)):
        total, rows = store.query_results(s, result_type, level1, False, page, size,
                                           archive_status=archive_status)
        items = []
        for r in rows:
            extra = store.list_item_extra(s, r)
            items.append({"id": r.id, "result_type": r.result_type, "level1": r.level1,
                          "level2": r.level2, "file_path": r.file_path, **extra})
        return {"total": total, "page": page, "size": size, "items": items}

    @app.get("/results/{rid}", dependencies=[Depends(require_key)])
    def result_detail(rid: int, s: Session = Depends(db)):
        from backend.models import ClassificationResult
        r = s.get(ClassificationResult, rid)
        if not r:
            raise HTTPException(404, "result not found")
        extra = store.result_extra(s, r)
        return {"id": r.id, "task_id": r.task_id, "file_path": r.file_path,
                "result_type": r.result_type, "relevance_status": r.relevance_status,
                "evidence_mode": r.evidence_mode, "level1": r.level1, "level2": r.level2,
                "candidate_scores": r.candidate_scores, "decision_source": r.decision_source,
                "classification_basis": r.classification_basis,
                "model_version": r.model_version, "taxonomy_version": r.taxonomy_version,
                **extra}

    @app.post("/results", status_code=201, dependencies=[Depends(require_key)])
    def ingest(payload: dict, s: Session = Depends(db)):
        try:
            r = store.save_result(s, payload)
        except ValueError as e:
            raise HTTPException(400, str(e))
        queued = store.pending_archive_message(s, r.file_id)
        if queued:
            app.state.archive_queue.enqueue(f"{queued['file_id']}:{queued['version']}", queued)
        return {"id": r.id}

    @app.get("/review/queue", dependencies=[Depends(require_key)])
    def review_queue(page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=200),
                     s: Session = Depends(db)):
        total, rows = store.query_results(s, None, None, True, page, size)
        return {"total": total, "items": [{"id": r.id, "file_path": r.file_path,
                                           "level1": r.level1, "reason": r.result_type} for r in rows]}

    @app.post("/review/{rid}", dependencies=[Depends(require_key)])
    def review(rid: int, patch: ReviewPatch, s: Session = Depends(db)):
        body = patch.model_dump()
        if body.get("decision"):
            try:
                out = store.apply_review_decision(s, rid, body)
            except LookupError:
                raise HTTPException(404, "review item not found or not in MANUAL_REVIEW")
            except RuntimeError:
                raise HTTPException(409, "version conflict")
            except ValueError as e:
                raise HTTPException(400, str(e))
            queued = out.get("queue")
            if queued:
                app.state.archive_queue.enqueue(
                    f"{queued['file_id']}:{queued['version']}", queued)
            return {"id": out["id"], "reviewed_by": out["reviewed_by"], "to_version": out["to_version"]}
        r = store.apply_review(s, rid, body)
        if not r:
            raise HTTPException(404, "review item not found or not in MANUAL_REVIEW")
        return {"id": r.id, "reviewed_by": r.reviewed_by}

    @app.get("/taxonomy", dependencies=[Depends(require_key)])
    def taxonomy():
        from classifier.taxonomy_v3 import L1, L2
        counts = {k: len(v) for k, v in L2.items()}
        total = sum(counts.values())
        if total != 27 or len(L1) != 4:
            raise HTTPException(500, "taxonomy v3 unreadable")
        return {"taxonomy_version": "v3.0-phase1", "level1": list(L1.keys()),
                "l2_counts": counts, "total_l2": total}

    @app.get("/metrics")
    def metrics(s: Session = Depends(db)):
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(store.metrics_text(s), media_type="text/plain; version=0.0.4")

    @app.get("/archive/{file_id}", dependencies=[Depends(require_key)])
    def archive_status(file_id: str, s: Session = Depends(db)):
        view = store.archive_view(s, file_id)
        if not view:
            raise HTTPException(404, "archive decision not found")
        return view

    return app


def get_app() -> FastAPI:
    return build_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(get_app(), host="0.0.0.0", port=int(os.environ.get("API_PORT", "8000")))
