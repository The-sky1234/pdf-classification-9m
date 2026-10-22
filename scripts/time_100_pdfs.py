#!/usr/bin/env python3
"""Measure 100 scan PDFs: OCR the pages needed for a label, then run the v3 model.

The sample is generated here. Pages are flattened images, so there is no text
layer. OCR is Tesseract chi_sim on this CPU. The model is the 27-scene labeler
used by scripts/classify_documents.py. BGE and PaddleOCR are not on this host.

Key-content rule, same as the earlier capacity probe: read pages in order and
stop at 400 characters or 2 pages. Later pages are not OCR'd.

Usage:
  python scripts/time_100_pdfs.py --out /tmp/time_100_pdfs.json
"""
from __future__ import annotations
import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
CHAR_CAP = 400
PAGE_CAP = 2


def page_plan(n_files: int = 100) -> list[int]:
    """60 single-page scans, 30 two-page scans, and the rest five-page scans."""
    if n_files < 1:
        return []
    one = round(n_files * 0.60)
    two = round(n_files * 0.30)
    five = n_files - one - two
    return [1] * one + [2] * two + [5] * five


def pages_to_read(char_counts: list[int], char_cap: int = CHAR_CAP, page_cap: int = PAGE_CAP) -> int:
    got = 0
    used = 0
    for count in char_counts:
        used += 1
        got += count
        if got >= char_cap or used >= page_cap:
            break
    return used


def build_scans(root: Path, n_files: int) -> list[dict]:
    import pymupdf
    from classifier.taxonomy_v3 import all_l2
    root.mkdir(parents=True, exist_ok=True)
    scenes = all_l2()
    files = []
    for idx, n_pages in enumerate(page_plan(n_files)):
        l1, scene = scenes[idx % len(scenes)]
        title = f"关于{scene}的通知"
        body = "\n".join([
            "各有关单位：",
            f"为做好{scene}工作，现就有关事项通知如下。",
            "一、工作内容。请对照职责开展检查，形成问题清单和整改计划。",
            "二、时限要求。请于十个工作日内完成自查，不得漏报瞒报。",
            "三、报送方式。书面报送办公室，同步上传台账。",
        ])
        src = pymupdf.open()
        for pno in range(1, n_pages + 1):
            page = src.new_page(width=595, height=842)
            page.insert_font(fontname="noto", fontfile=FONT)
            heading = title if pno == 1 else f"{title}（续{pno}）"
            extra = body if pno == 1 else f"附件{pno - 1}\n{scene}明细。责任单位反馈整改进度和完成时限。"
            page.insert_text((64, 78), heading, fontname="noto", fontsize=16)
            page.insert_textbox(pymupdf.Rect(64, 110, 530, 800), extra, fontname="noto", fontsize=11)
        flat = pymupdf.open()
        for i in range(n_pages):
            pix = src[i].get_pixmap(dpi=150)
            page = flat.new_page(width=595, height=842)
            page.insert_image(page.rect, stream=pix.tobytes("png"))
        src.close()
        path = root / f"scan_{idx:03d}_{n_pages}p.pdf"
        flat.save(path)
        flat.close()
        files.append({"path": str(path), "pages": n_pages, "scene": scene, "level1": l1, "title": title})
    return files


def _ocr_png(png_path: str, timeout: float) -> tuple[float, str, bool]:
    env = os.environ.copy()
    env["OMP_NUM_THREADS"] = "1"
    env["OMP_THREAD_LIMIT"] = "1"
    t = time.perf_counter()
    try:
        proc = subprocess.run(
            ["tesseract", png_path, "stdout", "-l", "chi_sim+eng", "--psm", "6"],
            capture_output=True, check=False, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return timeout, "", True
    return time.perf_counter() - t, proc.stdout.decode("utf-8", "replace"), False


def _render(pdf_path: str, page_index: int, dest: str) -> float:
    import pymupdf
    t = time.perf_counter()
    doc = pymupdf.open(pdf_path)
    try:
        pix = doc[page_index].get_pixmap(dpi=150)
        pix.save(dest)
    finally:
        doc.close()
    return time.perf_counter() - t


def _one_page(pdf_path: str, page_index: int, dest: str, timeout: float) -> dict:
    render_s = _render(pdf_path, page_index, dest)
    ocr_s, text, timed_out = _ocr_png(dest, timeout)
    return {"render_s": render_s, "ocr_s": ocr_s, "text": text, "timed_out": timed_out,
            "chars": len("".join(text.split()))}


def _run_pages(jobs: list[tuple[str, int, str]], workers: int, timeout: float) -> list[dict]:
    if not jobs:
        return []
    out: list[dict | None] = [None] * len(jobs)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_one_page, pdf, pidx, dest, timeout): i
                for i, (pdf, pidx, dest) in enumerate(jobs)}
        for fut in as_completed(futs):
            out[futs[fut]] = fut.result()
    return out


def measure(files: list[dict], work: Path, workers: int, timeout: float) -> dict:
    from classifier.backends import KeywordEmbedding
    from classifier.config import Thresholds
    from classifier.v3_label import label_v3
    work.mkdir(parents=True, exist_ok=True)
    warm_pdf = files[0]["path"]
    warm = work / "_warm.png"
    _one_page(warm_pdf, 0, str(warm), timeout)

    emb = KeywordEmbedding()
    th = Thresholds(calibrated=False)
    page_times = []
    label_times = []
    timeouts = 0
    scene_hits = 0
    label_reasons: dict[str, int] = {}
    key_pages = []
    t0 = time.perf_counter()
    for wave in (0, 1):
        jobs = []
        owners = []
        for item in files:
            if item["pages"] <= wave:
                continue
            if wave == 1 and item.get("_stop"):
                continue
            dest = work / f"{Path(item['path']).stem}_p{wave}.png"
            jobs.append((item["path"], wave, str(dest)))
            owners.append(item)
        results = _run_pages(jobs, workers, timeout)
        for item, result in zip(owners, results):
            page_times.append(result["render_s"] + result["ocr_s"])
            timeouts += int(result["timed_out"])
            item.setdefault("_text", [])
            item["_text"].append(result["text"])
            item["_chars"] = item.get("_chars", 0) + result["chars"]
            if result["chars"] >= CHAR_CAP or wave + 1 >= PAGE_CAP:
                item["_stop"] = True
    for item in files:
        parsed = {"ok": True, "file_path": item["path"], "pdf_type": "SCAN_PDF",
                  "title": "", "headings": [], "evidence_mode": "OCR_BODY",
                  "pages": [{"page_no": i + 1, "text": text} for i, text in enumerate(item["_text"])]}
        t = time.perf_counter()
        labeled = label_v3(parsed, emb, th)
        label_times.append(time.perf_counter() - t)
        reason = labeled.get("reason") or labeled.get("result_type")
        label_reasons[reason] = label_reasons.get(reason, 0) + 1
        scene_hits += int(labeled.get("level2") == item["scene"])
        key_pages.append(len(item["_text"]))
    elapsed = time.perf_counter() - t0
    full_pages = sum(item["pages"] for item in files)
    per_page = statistics.median(page_times) if page_times else 0.0
    return {"files": len(files),
            "workers": workers,
            "ocr": "tesseract 5 chi_sim+eng",
            "model": "v3 keyword scene labeler",
            "dpi": 150,
            "wall_seconds": round(elapsed, 2),
            "seconds_per_file": round(elapsed / len(files), 3),
            "key_pages_total": sum(key_pages),
            "key_pages_mean": round(sum(key_pages) / len(files), 3),
            "full_pages_total": full_pages,
            "page_seconds_p50": round(per_page, 3),
            "ocr_timeouts": timeouts,
            "label_ms_p50": round(statistics.median(label_times) * 1000, 3),
            "label_seconds_total": round(sum(label_times), 3),
            "scene_hit_rate": round(scene_hits / len(files), 3),
            "label_reasons": label_reasons,
            "single_core_key_seconds": round(sum(page_times) + sum(label_times), 2),
            "full_ocr_single_core_seconds_estimate": round(per_page * full_pages + sum(label_times), 2)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="实测一百份扫描 PDF 的 OCR 加模型判断时间")
    parser.add_argument("--files", type=int, default=100)
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--work", default="")
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)
    work = Path(args.work) if args.work else Path("/tmp/time_100_pdfs")
    files = build_scans(work / "pdfs", args.files)
    result = measure(files, work / "png", args.workers, args.timeout)
    result["built_files"] = len(files)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
