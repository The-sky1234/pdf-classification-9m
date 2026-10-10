from __future__ import annotations
import os
from pathlib import Path
from fastapi import FastAPI, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from backend.models import make_engine, make_session, init_db
from backend import store
from backend.schemas import ScanRequest, ReviewPatch
from backend.auth import require_key


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
    app = FastAPI(title="900万PDF智能分类系统API", version="v1.0-planner")
    app.state.sessions = sessions

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
        found: list[str] = []
        for dp, _, fns in os.walk(req.root):
            for fn in fns:
                if fn.lower().endswith(".pdf"):
                    found.append(os.path.join(dp, fn))
                if len(found) >= req.limit:
                    break
            if len(found) >= req.limit:
                break
        tasks = [store.create_task(s, p) for p in found]
        return {"accepted": len(tasks), "task_ids": [t.task_id for t in tasks]}

    @app.get("/tasks/{task_id}", dependencies=[Depends(require_key)])
    def task_status(task_id: str, s: Session = Depends(db)):
        t = store.get_task(s, task_id)
        if not t:
            raise HTTPException(404, "task not found")
        return {"task_id": t.task_id, "file_path": t.file_path, "stage": t.stage,
                "status": t.status, "retry_count": t.retry_count,
                "error_code": t.error_code, "error_msg": t.error_msg}

    @app.get("/results", dependencies=[Depends(require_key)])
    def results(result_type: str | None = None, level1: str | None = None,
                page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=200),
                s: Session = Depends(db)):
        total, rows = store.query_results(s, result_type, level1, False, page, size)
        return {"total": total, "page": page, "size": size,
                "items": [{"id": r.id, "result_type": r.result_type, "level1": r.level1,
                           "level2": r.level2, "file_path": r.file_path} for r in rows]}

    @app.get("/results/{rid}", dependencies=[Depends(require_key)])
    def result_detail(rid: int, s: Session = Depends(db)):
        from backend.models import ClassificationResult
        r = s.get(ClassificationResult, rid)
        if not r:
            raise HTTPException(404, "result not found")
        return {"id": r.id, "task_id": r.task_id, "file_path": r.file_path,
                "result_type": r.result_type, "relevance_status": r.relevance_status,
                "evidence_mode": r.evidence_mode, "level1": r.level1, "level2": r.level2,
                "candidate_scores": r.candidate_scores, "decision_source": r.decision_source,
                "classification_basis": r.classification_basis,
                "model_version": r.model_version, "taxonomy_version": r.taxonomy_version}

    @app.post("/results", status_code=201, dependencies=[Depends(require_key)])
    def ingest(payload: dict, s: Session = Depends(db)):
        try:
            r = store.save_result(s, payload)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"id": r.id}

    @app.get("/review/queue", dependencies=[Depends(require_key)])
    def review_queue(page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=200),
                     s: Session = Depends(db)):
        total, rows = store.query_results(s, None, None, True, page, size)
        return {"total": total, "items": [{"id": r.id, "file_path": r.file_path,
                                           "level1": r.level1} for r in rows]}

    @app.post("/review/{rid}", dependencies=[Depends(require_key)])
    def review(rid: int, patch: ReviewPatch, s: Session = Depends(db)):
        r = store.apply_review(s, rid, patch.model_dump())
        if not r:
            raise HTTPException(404, "review item not found or not in MANUAL_REVIEW")
        return {"id": r.id, "reviewed_by": r.reviewed_by}

    @app.get("/taxonomy", dependencies=[Depends(require_key)])
    def taxonomy():
        from classifier.knowledge import load_taxonomy
        kb = load_taxonomy("contracts/taxonomy.yaml")
        return {"taxonomy_version": kb["taxonomy_version"],
                "level1": list(kb["level1"].keys()),
                "l2_counts": {k: len(v) for k, v in kb["l2_by_l1"].items()}}

    return app


def get_app() -> FastAPI:
    return build_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(get_app(), host="0.0.0.0", port=int(os.environ.get("API_PORT", "8000")))
