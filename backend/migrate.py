"""按文件名顺序应用 backend/migrations/*.sql。

schema_migrations 是重复执行的权威保护：已记录的文件整份跳过。
连接由调用方提供，本模块不读取密码、不连接任何库。
"""
from __future__ import annotations

from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

SCHEMA_MIGRATIONS_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  id BIGSERIAL PRIMARY KEY,
  version TEXT NOT NULL UNIQUE,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def split_sql(script: str) -> list[str]:
    """按分号切分 SQL，忽略字符串、美元引号和注释里的分号。"""
    statements: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(script)
    dollar: str | None = None
    in_single = False
    in_line = False
    in_block = False
    while i < n:
        ch = script[i]
        nxt = script[i + 1] if i + 1 < n else ""
        if in_line:
            buf.append(ch)
            i += 1
            if ch == "\n":
                in_line = False
            continue
        if in_block:
            if ch == "*" and nxt == "/":
                buf.append("*/")
                i += 2
                in_block = False
            else:
                buf.append(ch)
                i += 1
            continue
        if dollar is not None:
            if script.startswith(dollar, i):
                buf.append(dollar)
                i += len(dollar)
                dollar = None
            else:
                buf.append(ch)
                i += 1
            continue
        if in_single:
            buf.append(ch)
            i += 1
            if ch == "'":
                if nxt == "'":
                    buf.append(nxt)
                    i += 1
                else:
                    in_single = False
            continue
        if ch == "-" and nxt == "-":
            buf.append("--")
            i += 2
            in_line = True
            continue
        if ch == "/" and nxt == "*":
            buf.append("/*")
            i += 2
            in_block = True
            continue
        if ch == "'":
            buf.append(ch)
            i += 1
            in_single = True
            continue
        if ch == "$":
            j = i + 1
            while j < n and (script[j].isalnum() or script[j] == "_"):
                j += 1
            if j < n and script[j] == "$":
                tag = script[i : j + 1]
                buf.append(tag)
                i = j + 1
                dollar = tag
                continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if _executable(stmt):
                statements.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if _executable(tail):
        statements.append(tail)
    return statements


def _executable(stmt: str) -> bool:
    if not stmt:
        return False
    kept: list[str] = []
    i = 0
    n = len(stmt)
    dollar: str | None = None
    in_single = False
    in_line = False
    in_block = False
    while i < n:
        ch = stmt[i]
        nxt = stmt[i + 1] if i + 1 < n else ""
        if in_line:
            i += 1
            if ch == "\n":
                in_line = False
            continue
        if in_block:
            if ch == "*" and nxt == "/":
                i += 2
                in_block = False
            else:
                i += 1
            continue
        if dollar is not None:
            if stmt.startswith(dollar, i):
                kept.append(dollar)
                i += len(dollar)
                dollar = None
            else:
                kept.append(ch)
                i += 1
            continue
        if in_single:
            kept.append(ch)
            i += 1
            if ch == "'":
                if nxt == "'":
                    kept.append(nxt)
                    i += 1
                else:
                    in_single = False
            continue
        if ch == "-" and nxt == "-":
            i += 2
            in_line = True
            continue
        if ch == "/" and nxt == "*":
            i += 2
            in_block = True
            continue
        if ch == "'":
            kept.append(ch)
            i += 1
            in_single = True
            continue
        if ch == "$":
            j = i + 1
            while j < n and (stmt[j].isalnum() or stmt[j] == "_"):
                j += 1
            if j < n and stmt[j] == "$":
                tag = stmt[i : j + 1]
                kept.append(tag)
                i = j + 1
                dollar = tag
                continue
        kept.append(ch)
        i += 1
    return bool("".join(kept).strip())


def apply_migrations(conn, migrations_dir: Path | None = None) -> list[str]:
    """执行尚未记录的迁移。每个脚本与其版本行在同一事务提交。返回本次新应用的文件名。"""
    directory = Path(migrations_dir) if migrations_dir is not None else MIGRATIONS_DIR
    applied_now: list[str] = []
    with conn.cursor() as cur:
        cur.execute(SCHEMA_MIGRATIONS_SQL)
    conn.commit()

    files = sorted(p for p in directory.glob("*.sql") if p.is_file())
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM schema_migrations")
        done = {row[0] for row in cur.fetchall()}

    for path in files:
        version = path.name
        if version in done:
            continue
        statements = split_sql(path.read_text(encoding="utf-8"))
        try:
            with conn.cursor() as cur:
                for idx, stmt in enumerate(statements, start=1):
                    try:
                        cur.execute(stmt)
                    except Exception as exc:
                        raise RuntimeError(
                            f"{version} statement {idx} failed: {exc}"
                        ) from exc
                cur.execute(
                    "INSERT INTO schema_migrations (version) VALUES (%s)",
                    (version,),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        applied_now.append(version)
    return applied_now
