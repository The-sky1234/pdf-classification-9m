#!/usr/bin/env python3
"""按六个阶段处理一个混合目录：文本、办公文档、网页、版式文件、图片和 PDF。

  python scripts/run_campaign.py --root /data/docs --archive /data/archive --report /data/archive/campaign.json

没有 --labels 时，校准阶段不会打开自动归档。--batch-id 用来续跑。
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


def main(argv: list[str] | None = None) -> int:
    from backend.models import init_db, make_engine, make_session
    from scheduler.campaign import run_campaign
    parser = argparse.ArgumentParser(description="混合文档的六个阶段")
    parser.add_argument("--root", required=True)
    parser.add_argument("--archive", required=True)
    parser.add_argument("--db", default=os.environ.get("DATABASE_URL", "sqlite:///./dev.db"))
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--batch-id", type=int, default=None)
    parser.add_argument("--labels", default=None, help="JSON 标注，用来对照校准，不自动生效")
    parser.add_argument("--report", default=None)
    parser.add_argument("--total-files", type=int, default=9_800_000)
    parser.add_argument("--calibrated", action="store_true")
    args = parser.parse_args(argv)
    if not os.path.isdir(args.root):
        print(f"源目录不存在：{args.root}", file=sys.stderr)
        return 2
    engine = make_engine(args.db)
    init_db(engine)
    db = make_session(engine)()
    try:
        report = run_campaign(
            db, os.path.abspath(args.root), os.path.abspath(args.archive),
            limit=args.limit, batch_id=args.batch_id, labels_path=args.labels,
            total_files=args.total_files, calibrated=True if args.calibrated else None)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        db.close()
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        Path(args.report).write_text(text + "\n", encoding="utf-8")
    print(text)
    phases = report["phases"]
    if not phases["1_knowledge"]["ready"] or not phases["2_validate"]["passed"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
