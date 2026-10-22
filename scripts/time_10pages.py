#!/usr/bin/env python3
"""Time 100 scan PDFs when every file OCRs all 10 pages, then one LLM call.

No 2-page cap and no 400-character cap. The model receives the OCR text of
all 10 pages. OCR is Tesseract chi_sim+eng at 150 dpi.

Usage:
  python scripts/time_10pages.py --work /tmp/time_10pages --out /tmp/time_10pages_ocr.json
  python scripts/time_10pages.py --llm http://127.0.0.1:18081 --ocr-json /tmp/time_10pages_ocr.json --out /tmp/time_10pages_llm.json
"""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
SCENES = [
    "消防监管", "矿山监管监察", "危险化学品监管", "石油开采安全监管", "安全生产执法和工贸监管",
    "调查评估和统计", "安全生产综合协调", "风险监测和综合减灾", "森林草原火灾防治", "防汛抗旱",
    "地质灾害", "防震减灾", "救灾和物资保障", "指挥调度", "救援力量", "预案管理", "综合办公",
    "人事管理", "队伍管理", "政策法规", "国际合作", "规划财务", "新闻宣传", "科技和信息化",
    "党建管理", "离退休干部管理", "纪检监察",
]


def build_scans(root: Path, n_files: int, n_pages: int) -> list[dict]:
    import pymupdf
    root.mkdir(parents=True, exist_ok=True)
    files = []
    for idx in range(n_files):
        scene = SCENES[idx % len(SCENES)]
        title = f"关于{scene}的通知"
        src = pymupdf.open()
        for pno in range(1, n_pages + 1):
            page = src.new_page(width=595, height=842)
            page.insert_font(fontname="noto", fontfile=FONT)
            heading = title if pno == 1 else f"{title}（第{pno}页）"
            body = "\n".join([
                "各有关单位：",
                f"第{pno}页。为做好{scene}工作，现就有关事项通知如下。",
                "一、工作内容。请对照职责开展检查，形成问题清单和整改计划。",
                "二、时限要求。请于十个工作日内完成自查，不得漏报瞒报。",
                "三、报送方式。书面报送办公室，同步上传台账。",
                f"四、本页补充。序号{pno}，责任单位反馈整改进度、责任人和完成时限。",
            ])
            page.insert_text((64, 78), heading, fontname="noto", fontsize=16)
            page.insert_textbox(pymupdf.Rect(64, 110, 530, 800), body, fontname="noto", fontsize=11)
        flat = pymupdf.open()
        for i in range(n_pages):
            pix = src[i].get_pixmap(dpi=150)
            page = flat.new_page(width=595, height=842)
            page.insert_image(page.rect, stream=pix.tobytes("png"))
        src.close()
        path = root / f"scan_{idx:03d}.pdf"
        flat.save(path)
        flat.close()
        files.append({"path": str(path), "pages": n_pages, "scene": scene, "title": title})
    return files


def _ocr_page(pdf_path: str, page_index: int, dest: str) -> dict:
    import pymupdf
    env = os.environ.copy()
    env["OMP_NUM_THREADS"] = "1"
    env["OMP_THREAD_LIMIT"] = "1"
    t = time.perf_counter()
    doc = pymupdf.open(pdf_path)
    try:
        pix = doc[page_index].get_pixmap(dpi=150)
        pix.save(dest)
    finally:
        doc.close()
    render_s = time.perf_counter() - t
    t = time.perf_counter()
    try:
        proc = subprocess.run(
            ["tesseract", dest, "stdout", "-l", "chi_sim+eng", "--psm", "6"],
            capture_output=True, check=False, timeout=45, env=env)
        timed_out = False
        text = proc.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        timed_out = True
        text = ""
    ocr_s = time.perf_counter() - t
    try:
        os.remove(dest)
    except OSError:
        pass
    return {"text": text, "render_s": render_s, "ocr_s": ocr_s, "timed_out": timed_out,
            "chars": len("".join(text.split()))}


def ocr_all(files: list[dict], work: Path, workers: int) -> dict:
    work.mkdir(parents=True, exist_ok=True)
    jobs = []
    for item in files:
        for pidx in range(item["pages"]):
            dest = work / f"{Path(item['path']).stem}_p{pidx}.png"
            jobs.append((item["path"], pidx, str(dest)))
    # Warm one page so the language data is not inside the timed section.
    _ocr_page(jobs[0][0], jobs[0][1], jobs[0][2])
    texts: dict[tuple[str, int], dict] = {}
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_ocr_page, pdf, pidx, dest): (pdf, pidx) for pdf, pidx, dest in jobs}
        done = 0
        for fut in as_completed(futs):
            key = futs[fut]
            texts[key] = fut.result()
            done += 1
            if done % 100 == 0:
                print(f"ocr {done}/{len(jobs)}", flush=True)
    elapsed = time.perf_counter() - t0
    rows = []
    timeouts = 0
    for item in files:
        pages = []
        chars = 0
        for pidx in range(item["pages"]):
            got = texts[(item["path"], pidx)]
            pages.append(got["text"])
            chars += got["chars"]
            timeouts += int(got["timed_out"])
        rows.append({"file": Path(item["path"]).stem, "scene": item["scene"], "pages": item["pages"],
                     "pages_read": len(pages), "chars": chars, "text": "\n".join(pages)})
    short = [r["file"] for r in rows if r["pages_read"] != r["pages"]]
    return {"files": len(rows), "pages_each": files[0]["pages"] if files else 0,
            "pages_total": sum(r["pages_read"] for r in rows), "ocr_timeouts": timeouts,
            "wall_seconds": round(elapsed, 2), "workers": workers,
            "chars_min": min(r["chars"] for r in rows),
            "chars_p50": sorted(r["chars"] for r in rows)[len(rows) // 2],
            "chars_max": max(r["chars"] for r in rows),
            "short_files": short, "rows": rows}


def _chat(url: str, system: str, user: str) -> tuple[float, str, dict]:
    payload = {"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
               "temperature": 0, "max_tokens": 32}
    req = urllib.request.Request(url.rstrip("/") + "/v1/chat/completions",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    t = time.perf_counter()
    with urllib.request.urlopen(req, timeout=300) as resp:
        body = json.loads(resp.read().decode())
    answer = body["choices"][0]["message"]["content"].strip()
    return time.perf_counter() - t, answer, body.get("usage") or {}


def label_all(ocr: dict, url: str) -> dict:
    system = "你是档案分类员。只能从用户给出的场景名中选一个。证据不够就回答 UNCERTAIN。只输出场景名或 UNCERTAIN，不要解释。"
    names = "\n".join(f"- {name}" for name in SCENES)
    rows = ocr["rows"]
    def user_text(row: dict) -> str:
        body = " ".join(row["text"].split())
        return f"场景名：\n{names}\n\n正文：\n{body}"
    warm_s, warm_ans, warm_usage = _chat(url, system, user_text(rows[0]))
    print(f"warmup {warm_s:.2f}s {warm_ans!r} {warm_usage}", flush=True)
    out = []
    t0 = time.perf_counter()
    for i, row in enumerate(rows):
        dt, answer, usage = _chat(url, system, user_text(row))
        hit = next((name for name in SCENES if name in answer), "UNCERTAIN" if "UNCERTAIN" in answer else "OTHER")
        out.append({"file": row["file"], "scene": row["scene"], "pages_read": row["pages_read"],
                    "chars": row["chars"], "seconds": round(dt, 3), "answer": answer, "hit": hit,
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens")})
        if i < 3 or (i + 1) % 10 == 0:
            print(f"{i+1}/{len(rows)} {dt:.2f}s tokens={usage.get('prompt_tokens')} {answer!r}", flush=True)
    wall = time.perf_counter() - t0
    secs = sorted(r["seconds"] for r in out)
    prompts = sorted((r["prompt_tokens"] or 0) for r in out)
    matched = sum(1 for r in out if r["hit"] == r["scene"])
    return {"model": "Qwen/Qwen2.5-3B-Instruct Q4_K_M", "files": len(out),
            "pages_each": ocr.get("pages_each"), "pages_total": ocr.get("pages_total"),
            "ocr_wall_seconds": ocr.get("wall_seconds"), "ocr_workers": ocr.get("workers"),
            "chars_p50": ocr.get("chars_p50"), "chars_min": ocr.get("chars_min"),
            "chars_max": ocr.get("chars_max"), "llm_warmup_seconds": round(warm_s, 3),
            "llm_wall_seconds": round(wall, 2),
            "llm_seconds_p50": secs[len(secs) // 2], "llm_seconds_min": secs[0],
            "llm_seconds_max": secs[-1], "prompt_tokens_p50": prompts[len(prompts) // 2],
            "prompt_tokens_min": prompts[0], "prompt_tokens_max": prompts[-1],
            "exact_scene_match": matched,
            "sequential_seconds": round(ocr.get("wall_seconds", 0) + wall, 2),
            "all_files_read_every_page": all(r["pages_read"] == ocr.get("pages_each") for r in out),
            "rows": out}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="每份扫描件读取全部 10 页后再计时")
    parser.add_argument("--files", type=int, default=100)
    parser.add_argument("--pages", type=int, default=10)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--work", default="/tmp/time_10pages")
    parser.add_argument("--ocr-json", default="")
    parser.add_argument("--llm", default="")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    if args.ocr_json:
        ocr = json.loads(Path(args.ocr_json).read_text(encoding="utf-8"))
    else:
        files = build_scans(Path(args.work) / "pdfs", args.files, args.pages)
        ocr = ocr_all(files, Path(args.work) / "png", args.workers)
        if not args.llm:
            Path(args.out).write_text(json.dumps(ocr, ensure_ascii=False), encoding="utf-8")
            print(json.dumps({k: v for k, v in ocr.items() if k != "rows"}, ensure_ascii=False, indent=2))
            return 0
    if not args.llm:
        raise SystemExit("已有 OCR 结果时必须提供 --llm")
    summary = label_all(ocr, args.llm)
    Path(args.out).write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "rows"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
