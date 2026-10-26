#!/usr/bin/env python3
"""扫描一个目录，对其中各类文档做阶段一分类并归档。

用法：
  python scripts/classify_documents.py --root /data/docs --archive /data/archive

归档目录必须在源目录外面。去重默认视为已经完成。
THRESHOLDS_CALIBRATED=1 时，达到现有直接通过阈值的业务文件才进入 27 个场景目录。
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_local_env() -> None:
    path = ROOT / ".env"
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


def main(argv: list[str] | None = None) -> int:
    _load_local_env()
    from backend.models import init_db, make_engine, make_session
    from scheduler.scan_classify import run
    parser = argparse.ArgumentParser(description="扫描目录并完成文档分类归档")
    parser.add_argument("--root", required=True, help="只读源目录")
    parser.add_argument("--archive", required=True, help="归档根目录，不能放在源目录里面")
    parser.add_argument("--db", default=os.environ.get("DATABASE_URL", "sqlite:///./dev.db"))
    parser.add_argument("--limit", type=int, default=200, help="每批登记数量")
    parser.add_argument("--batch-id", type=int, default=None, help="续跑已有批次")
    parser.add_argument("--calibrated", action="store_true",
                        help="允许达到阈值的业务标签写入场景目录")
    args = parser.parse_args(argv)
    if not os.path.isdir(args.root):
        print(f"源目录不存在：{args.root}", file=sys.stderr)
        return 2
    engine = make_engine(args.db)
    init_db(engine)
    db = make_session(engine)()
    try:
        summary = run(db, os.path.abspath(args.root), os.path.abspath(args.archive),
                      limit=args.limit, batch_id=args.batch_id,
                      calibrated=True if args.calibrated else None)
    except (ValueError, RuntimeError) as e:
        print(str(e), file=sys.stderr)
        return 2
    finally:
        db.close()
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
