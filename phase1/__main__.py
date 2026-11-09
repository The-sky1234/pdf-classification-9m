"""波次 0 命令行。

    python -m phase1 cpu --masters 主件清单.txt --out 结论.jsonl --archive-root D:\\归档
"""
from __future__ import annotations

import argparse
import json

from phase1.cpu import run_cpu_wave


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="phase1")
    sub = parser.add_subparsers(dest="cmd", required=True)
    cpu = sub.add_parser("cpu", help="波次 0：抽文字、规则判断，可选复制已定论的文件")
    cpu.add_argument("--masters", required=True, help="主件清单，一行一个路径，或带「完整路径」列的 CSV")
    cpu.add_argument("--out", required=True, help="结论 jsonl")
    cpu.add_argument("--archive-root", default="", help="给出才复制。不给就只写结论")
    args = parser.parse_args(argv)
    if args.cmd == "cpu":
        counts = run_cpu_wave(args.masters, args.out, args.archive_root or None)
        print(json.dumps(counts, ensure_ascii=False))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
