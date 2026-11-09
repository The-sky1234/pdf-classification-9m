"""阶段一命令行。数据库地址读 DATABASE_URL，或用 --db 指定。

服务器流程（docker compose 里各容器跑的就是这些命令）：

    python -m phase1 init-db
    python -m phase1 load --masters /data/input/masters.csv
    python -m phase1 work --stage cpu --stage archive --procs 16      # 波次 0 + 归档，CPU
    python -m phase1 work --stage ocr                                  # 波次 1，GPU
    python -m phase1 work --stage classify                             # 波次 2，GPU
    python -m phase1 status
    python -m phase1 export --out /data/out/decisions.jsonl
    python -m phase1 requeue [--stage cpu]                             # failed 的重新排队

单机试跑（不连数据库）：

    python -m phase1 cpu --masters 主件清单.txt --out 结论.jsonl --archive-root D:\\归档
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from phase1 import ledger


def _db(args) -> str:
    url = args.db or os.environ.get("DATABASE_URL", "")
    if not url:
        raise SystemExit("没有数据库地址：设置 DATABASE_URL 或传 --db")
    return url


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="phase1")
    parser.add_argument("--db", default="", help="默认读 DATABASE_URL")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="建表/迁移（多个容器同时执行也只迁移一次）")

    load = sub.add_parser("load", help="把主件清单灌进台账，可重复执行")
    load.add_argument("--masters", required=True, help="masters.csv，或一行一个路径的文本")

    work = sub.add_parser("work", help="跑一个或几个波次")
    work.add_argument("--stage", action="append", choices=ledger.WORK_STAGES, required=True)
    work.add_argument("--procs", type=int, default=1)
    work.add_argument("--batch", type=int, default=0)
    work.add_argument("--lease", type=float, default=0, help="租约秒数，默认按波次")
    work.add_argument("--timeout", type=int, default=0, help="单文件超时秒数，默认按波次")
    work.add_argument("--max-attempts", type=int, default=3)
    work.add_argument("--until-empty", action="store_true", help="领不到任务就退出（默认一直等）")
    work.add_argument("--archive-root", default="", help="默认读 ARCHIVE_ROOT")

    sub.add_parser("status", help="各阶段、各结论的数量")

    export = sub.add_parser("export", help="导出全部结论为 jsonl")
    export.add_argument("--out", required=True)

    requeue = sub.add_parser("requeue", help="failed 的行回到失败前的阶段重跑")
    requeue.add_argument("--stage", default="", choices=("",) + ledger.WORK_STAGES)

    cpu = sub.add_parser("cpu", help="单机试跑波次 0：抽文字、规则判断，可选复制已定论的文件")
    cpu.add_argument("--masters", required=True, help="主件清单，一行一个路径，或带「完整路径」列的 CSV")
    cpu.add_argument("--out", required=True, help="结论 jsonl")
    cpu.add_argument("--archive-root", default="", help="给出才复制。不给就只写结论")

    args = parser.parse_args(argv)
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(process)d %(levelname)s %(message)s")

    if args.cmd == "cpu":
        from phase1.cpu import run_cpu_wave

        counts = run_cpu_wave(args.masters, args.out, args.archive_root or None)
        print(json.dumps(counts, ensure_ascii=False))
        return 0

    url = _db(args)
    if args.cmd == "init-db":
        ledger.ensure_schema(ledger.make_engine(url))
        print("schema ok")
        return 0
    if args.cmd == "load":
        from phase1.masters import iter_master_rows

        engine = ledger.make_engine(url)
        ledger.ensure_schema(engine)
        print(json.dumps(ledger.load_masters(engine, iter_master_rows(args.masters)), ensure_ascii=False))
        return 0
    if args.cmd == "work":
        from phase1.waves import run_workers

        kw = {"archive_root": args.archive_root or os.environ.get("ARCHIVE_ROOT", "")}
        return run_workers(
            url, args.stage, args.procs, batch=args.batch or None, lease_s=args.lease or None,
            timeout_s=args.timeout or None, max_attempts=args.max_attempts, until_empty=args.until_empty,
            handler_kw=kw,
        )
    if args.cmd == "status":
        print(json.dumps(ledger.summary(ledger.make_engine(url)), ensure_ascii=False, indent=1))
        return 0
    if args.cmd == "export":
        n = 0
        with open(args.out + ".part", "w", encoding="utf-8") as out:
            for row in ledger.iter_all(ledger.make_engine(url)):
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
                n += 1
        os.replace(args.out + ".part", args.out)
        print(f"exported {n} rows to {args.out}")
        return 0
    if args.cmd == "requeue":
        print(json.dumps({"requeued": ledger.requeue_failed(ledger.make_engine(url), args.stage or None)}))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
