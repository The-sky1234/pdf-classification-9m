"""阶段一的任务台账：每份主件一行，记录它走到哪个波次、结论和归档结果。

生产用 PostgreSQL（表由 backend/migrations/004_phase1_ledger.sql 建），测试和单机试跑可以用 SQLite。

领取任务用租约：一次领一批，写上 lease_owner 和 lease_until。
- PostgreSQL 用 SELECT … FOR UPDATE SKIP LOCKED，多进程、多容器并发领取互不重复；
- 进程崩溃不用人管，租约到期后别的进程会重新领到；
- 写回结果时校验 lease_owner，过期的旧进程写不回来；
- 同一行被领取超过 max_attempts 次还没完成，就转 failed 等人看。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import zlib
from typing import Iterable

from sqlalchemy import (BigInteger, Column, Float, Index, Integer, LargeBinary, MetaData, String, Table, Text,
                        create_engine, event, func, or_, select, update)
from sqlalchemy.engine import Engine

STAGES = ("cpu", "ocr", "classify", "archive", "done", "failed")
WORK_STAGES = ("cpu", "ocr", "classify", "archive")
EVIDENCE_CHARS = int(os.environ.get("EVIDENCE_CHARS", "2000"))
SNIPPET_CHARS = 200

metadata = MetaData()
files = Table(
    "phase1_files",
    metadata,
    Column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True),
    Column("path_key", String(40), nullable=False, unique=True),
    Column("source_path", Text, nullable=False),
    Column("source_from", String(32)),
    Column("extension", String(16)),
    Column("size_bytes", BigInteger),
    Column("stage", String(16), nullable=False),
    Column("failed_stage", String(16)),
    Column("lease_owner", String(80)),
    Column("lease_until", Float),
    Column("attempts", Integer, nullable=False, default=0),
    Column("archive_type", String(16)),
    Column("decision_status", String(16)),
    Column("reason", Text),
    Column("level1", String(16)),
    Column("level2", String(32)),
    Column("target_dir", Text),
    Column("page_count", Integer),
    Column("ocr_pages", Text),
    Column("evidence", LargeBinary),
    Column("snippet", Text),
    Column("scores", Text),
    Column("archive_path", Text),
    Column("archive_sha256", String(64)),
    Column("archive_bytes", BigInteger),
    Column("previous_archive_path", Text),
    Column("previous_archive_sha256", String(64)),
    Column("reviewed_by", String(64)),
    Column("reviewed_at", Float),
    Column("review_note", Text),
    Column("last_error", Text),
    Column("updated_at", Float, nullable=False),
)
Index("ix_phase1_files_stage_id", files.c.stage, files.c.id)


def path_key(path: str) -> str:
    return hashlib.sha1(path.encode("utf-8", "surrogateescape")).hexdigest()


def pack_text(text: str) -> bytes:
    return zlib.compress(text[:EVIDENCE_CHARS].encode("utf-8"), 6)


def unpack_text(blob: bytes | None) -> str:
    return zlib.decompress(blob).decode("utf-8") if blob else ""


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        engine = create_engine(url, connect_args={"timeout": 60})

        @event.listens_for(engine, "connect")
        def _sqlite_connect(dbapi_conn, _record):
            dbapi_conn.isolation_level = None
            dbapi_conn.execute("PRAGMA journal_mode=WAL")
            dbapi_conn.execute("PRAGMA busy_timeout=60000")

        @event.listens_for(engine, "begin")
        def _sqlite_begin(conn):
            conn.exec_driver_sql("BEGIN IMMEDIATE")

        return engine
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5, connect_args=PG_CONNECT_ARGS)


PG_CONNECT_ARGS = {"connect_timeout": 10, "keepalives": 1, "keepalives_idle": 60, "keepalives_interval": 10,
                   "keepalives_count": 5}


def wait_for_db(engine: Engine, timeout_s: float | None = None) -> None:
    """数据库重启、容器先后启动时等它起来，而不是直接退出。超时后抛出最后一次的错误。"""
    if timeout_s is None:
        timeout_s = float(os.environ.get("DB_WAIT_S", "180"))
    deadline = time.monotonic() + timeout_s
    delay = 1.0
    while True:
        try:
            with engine.connect() as conn:
                conn.exec_driver_sql("SELECT 1")
            return
        except Exception:  # noqa: BLE001 连不上的原因很多，统一重试到超时
            if time.monotonic() + delay > deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 15.0)


def ensure_schema(engine: Engine) -> None:
    """SQLite 直接建表。PostgreSQL 走迁移脚本，用 advisory lock 保证多个容器同时启动时只迁移一次。"""
    if engine.dialect.name == "sqlite":
        metadata.create_all(engine)
        return
    from backend.migrate import apply_migrations

    wait_for_db(engine)
    raw = engine.raw_connection()
    try:
        dbapi = raw.driver_connection if hasattr(raw, "driver_connection") else raw.connection
        with dbapi.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(727001)")
        try:
            apply_migrations(dbapi)
        finally:
            with dbapi.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(727001)")
            dbapi.commit()
    finally:
        raw.close()


def load_masters(engine: Engine, rows: Iterable[dict], batch: int = 5000) -> dict:
    """把主件清单灌进台账，stage=cpu。同一路径重复导入不会产生第二行。"""
    inserted = seen = 0
    buf: list[dict] = []
    now = time.time()

    def flush():
        nonlocal inserted
        if not buf:
            return
        with engine.begin() as conn:
            stmt = _insert_ignore(engine)(files).values(buf)
            inserted += conn.execute(stmt).rowcount or 0
        buf.clear()

    for row in rows:
        seen += 1
        path = row["source_path"]
        buf.append({
            "path_key": path_key(path),
            "source_path": path,
            "source_from": (row.get("source_from") or "")[:32] or None,
            "extension": os.path.splitext(path)[1].lower()[:16],
            "size_bytes": row.get("size_bytes"),
            "stage": "cpu",
            "attempts": 0,
            "updated_at": now,
        })
        if len(buf) >= batch:
            flush()
    flush()
    return {"seen": seen, "inserted": inserted, "already_loaded": seen - inserted}


def _insert_ignore(engine: Engine):
    if engine.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        return lambda table: _OnConflict(pg_insert(table))
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    return lambda table: _OnConflict(sqlite_insert(table))


class _OnConflict:
    def __init__(self, stmt):
        self.stmt = stmt

    def values(self, rows):
        return self.stmt.values(rows).on_conflict_do_nothing(index_elements=["path_key"])


def claim(engine: Engine, stage: str, owner: str, limit: int, lease_s: float, max_attempts: int = 3) -> list[dict]:
    if stage not in WORK_STAGES:
        raise ValueError(stage)
    now = time.time()
    with engine.begin() as conn:
        query = (
            select(files.c.id, files.c.attempts)
            .where(files.c.stage == stage, or_(files.c.lease_until.is_(None), files.c.lease_until < now))
            .order_by(files.c.id)
            .limit(limit)
        )
        if engine.dialect.name == "postgresql":
            query = query.with_for_update(skip_locked=True)
        picked = conn.execute(query).all()
        exhausted = [row.id for row in picked if row.attempts >= max_attempts]
        ids = [row.id for row in picked if row.attempts < max_attempts]
        if exhausted:
            conn.execute(
                update(files).where(files.c.id.in_(exhausted)).values(
                    stage="failed", failed_stage=stage, lease_owner=None, lease_until=None, updated_at=now,
                    last_error=func.coalesce(files.c.last_error, "") + f" | 超过 {max_attempts} 次未完成（{stage}）",
                )
            )
        if not ids:
            return []
        conn.execute(
            update(files).where(files.c.id.in_(ids)).values(
                lease_owner=owner, lease_until=now + lease_s, attempts=files.c.attempts + 1, updated_at=now,
            )
        )
        return [dict(row) for row in conn.execute(select(files).where(files.c.id.in_(ids)).order_by(files.c.id)).mappings()]


def finish(engine: Engine, owner: str, results: list[tuple[int, dict]]) -> int:
    """写回一批结果并释放租约。返回成功写回的行数；租约已经被别人接走的行不写。

    进入下一阶段时调用方要带上 attempts=0，原地重试时不要带。
    """
    now = time.time()
    done = 0
    with engine.begin() as conn:
        for row_id, values in results:
            values = dict(values)
            values.setdefault("lease_until", None)
            values.update(lease_owner=None, updated_at=now)
            res = conn.execute(
                update(files).where(files.c.id == row_id, files.c.lease_owner == owner).values(**values)
            )
            done += res.rowcount or 0
    return done


def release(engine: Engine, owner: str, ids: list[int]) -> None:
    """进程要退出时，把领了还没处理的行还回去，不算一次尝试。"""
    with engine.begin() as conn:
        conn.execute(update(files).where(files.c.id.in_(ids), files.c.lease_owner == owner).values(
            lease_owner=None, lease_until=None, attempts=files.c.attempts - 1, updated_at=time.time()))


def retry_later(row: dict, error: str, max_attempts: int, backoff_s: float) -> dict:
    """可重试的错误：退避后再领；次数用完转 failed。"""
    if row["attempts"] >= max_attempts:
        return {"stage": "failed", "failed_stage": row["stage"], "last_error": error[:2000]}
    return {"stage": row["stage"], "last_error": error[:2000],
            "lease_until": time.time() + backoff_s * (2 ** max(0, row["attempts"] - 1))}


def fail(row: dict, error: str) -> dict:
    return {"stage": "failed", "failed_stage": row["stage"], "last_error": error[:2000]}


def requeue_failed(engine: Engine, stage: str | None = None) -> int:
    with engine.begin() as conn:
        cond = [files.c.stage == "failed"]
        if stage:
            cond.append(files.c.failed_stage == stage)
        res = conn.execute(
            update(files).where(*cond).values(
                stage=func.coalesce(files.c.failed_stage, "cpu"), failed_stage=None, attempts=0,
                lease_owner=None, lease_until=None, updated_at=time.time(),
            )
        )
        return res.rowcount or 0


def decision_values(decision) -> dict:
    """judge 的结论 → 台账字段。已定论的进入 archive 阶段，等归档进程复制。"""
    d = decision.to_dict() if hasattr(decision, "to_dict") else dict(decision)
    values = {
        "archive_type": d["archive_type"],
        "decision_status": d["decision_status"],
        "reason": d["reason"],
        "level1": d.get("level1"),
        "level2": d.get("level2"),
        "target_dir": d["target_dir"],
        "page_count": d.get("page_count") or None,
        "scores": json.dumps(d.get("scores") or {}, ensure_ascii=False) if d.get("scores") else None,
        "stage": "archive",
        "evidence": None,
        "ocr_pages": None,
        "last_error": None,
        "attempts": 0,
    }
    if not values["page_count"]:
        values.pop("page_count")
    return values


def summary(engine: Engine) -> dict:
    with engine.connect() as conn:
        by_stage = {row[0]: row[1] for row in conn.execute(
            select(files.c.stage, func.count()).group_by(files.c.stage))}
        by_type = {f"{row[0]}/{row[1]}": row[2] for row in conn.execute(
            select(files.c.archive_type, files.c.reason, func.count())
            .where(files.c.archive_type.is_not(None))
            .group_by(files.c.archive_type, files.c.reason))}
        leased = conn.execute(select(func.count()).where(files.c.lease_until > time.time(),
                                                         files.c.lease_owner.is_not(None))).scalar_one()
    return {"total": sum(by_stage.values()), "by_stage": by_stage, "by_decision": by_type, "leased": leased}


def get(engine: Engine, row_id: int) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(select(files).where(files.c.id == row_id)).mappings().first()
    return _public(row) if row else None


def list_rows(engine: Engine, *, stage: str | None = None, archive_type: str | None = None,
              reason: str | None = None, page: int = 1, size: int = 50) -> dict:
    cond = []
    if stage:
        cond.append(files.c.stage == stage)
    if archive_type:
        cond.append(files.c.archive_type == archive_type)
    if reason:
        cond.append(files.c.reason == reason)
    with engine.connect() as conn:
        total = conn.execute(select(func.count()).select_from(files).where(*cond)).scalar_one()
        rows = conn.execute(select(files).where(*cond).order_by(files.c.id)
                            .offset((page - 1) * size).limit(size)).mappings().all()
    return {"total": total, "page": page, "size": size, "items": [_public(r) for r in rows]}


def iter_all(engine: Engine, batch: int = 10000):
    last = 0
    while True:
        with engine.connect() as conn:
            rows = conn.execute(select(files).where(files.c.id > last).order_by(files.c.id).limit(batch)).mappings().all()
        if not rows:
            return
        for row in rows:
            yield _public(row)
        last = rows[-1]["id"]


def _public(row) -> dict:
    out = {k: v for k, v in dict(row).items() if k not in ("evidence", "path_key")}
    if out.get("scores"):
        out["scores"] = json.loads(out["scores"])
    if out.get("ocr_pages"):
        out["ocr_pages"] = json.loads(out["ocr_pages"])
    return out


def review(engine: Engine, row_id: int, *, reviewer: str, archive_type: str, level1: str | None = None,
           level2: str | None = None, reason: str | None = None, note: str = "") -> dict:
    """人工复核改判。只改已经落地（done）的行；改完转回 archive 阶段，由归档进程搬到新目录并删掉旧副本。"""
    from classifier.taxonomy_v3 import target_dir

    if archive_type == "PENDING":
        raise ValueError("复核结论不能还是待复核")
    if archive_type == "PATENT":
        level1 = level2 = None
        reason = "PATENT"
    elif archive_type == "BUSINESS":
        reason = "BUSINESS"
    elif archive_type == "LOW_VALUE":
        level1 = level2 = None
    rel = target_dir(archive_type, None, level1, level2, reason if archive_type == "LOW_VALUE" else None)
    now = time.time()
    with engine.begin() as conn:
        row = conn.execute(select(files).where(files.c.id == row_id)).mappings().first()
        if row is None:
            raise LookupError(row_id)
        if row["stage"] != "done":
            raise RuntimeError(f"这一行还在 {row['stage']} 阶段，等它落地以后再复核")
        conn.execute(update(files).where(files.c.id == row_id, files.c.stage == "done").values(
            archive_type=archive_type, decision_status="REVIEWED", reason=reason, level1=level1, level2=level2,
            target_dir=rel, stage="archive", attempts=0,
            previous_archive_path=row["archive_path"], previous_archive_sha256=row["archive_sha256"],
            reviewed_by=reviewer[:64], reviewed_at=now, review_note=note or None, updated_at=now,
        ))
    return get(engine, row_id)
