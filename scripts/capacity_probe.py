#!/usr/bin/env python3
"""Time a synthetic sample, then project 9.8 million files over 90 days.

The sample is generated here: Chinese notices with a real text layer, and the
same pages flattened to images so the scan path has no text layer. OCR is
Tesseract chi_sim on this CPU. There is no GPU and no PaddleOCR in this run,
so the OCR rate is a clean-scan CPU floor, not a production GPU number.

Usage:
  python scripts/capacity_probe.py --out /tmp/capacity_probe.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
TOTAL_FILES = 9_800_000
DAYS = 90
# Hardware report baseline: text 50% + complex 10% keep a text layer;
# scan 35% + image 5% need OCR. Complex layout is not re-measured here.
ASSUMED_SCAN_RATIO = 0.40

TOPICS = [
    ("关于开展危险化学品重大危险源专项排查的通知",
     "危险化学品安全生产监管", "各有关企业要对照重大危险源清单开展隐患排查，限期整改，并配合执法检查。"),
    ("市应急管理局2026年防汛抗旱工作要点",
     "防汛抗旱", "请沿江各区落实江河汛情监测、暴雨洪水风险防范和抗旱物资准备。"),
    ("森林草原火灾防治专项督办单",
     "森林草原火灾防治", "林区和草原火险等级升高，须加强巡护、火源管控和预警信息发布。"),
    ("地质灾害隐患点巡查记录",
     "地质灾害", "对滑坡、泥石流和崩塌隐患点进行雨前雨后巡查，发现险情立即上报。"),
    ("生产安全事故调查处理意见",
     "事故调查统计", "调查组认定事故经过、直接原因和管理漏洞，并提出责任追究建议。"),
    ("应急预案备案与演练计划",
     "应急预案管理", "请于本月底前完成专项预案修订、备案，并报送桌面演练安排。"),
    ("救援队伍装备补强申请",
     "应急救援力量", "申请补充破拆、通信和个体防护装备，用于专业救援队伍能力提升。"),
    ("机关党建和人事工作月报",
     "党建与人事", "本月完成支部学习、干部考核和岗位聘用，不涉及具体执法事项。"),
    ("救灾物资调拨通知",
     "救灾物资保障", "向受灾县调拨帐篷、折叠床和生活类救灾物资，做好接收和发放台账。"),
    ("综合办公会议纪要",
     "机关综合办公", "会议研究办文办会和后勤事项。专业监管议题另行按主责业务归档。"),
]


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _bigram_recall(src: str, ocr: str) -> float:
    a, b = _norm(src), _norm(ocr)
    if len(a) < 2:
        return 1.0
    ca = Counter(a[i:i + 2] for i in range(len(a) - 1))
    cb = Counter(b[i:i + 2] for i in range(len(b) - 1))
    hit = sum(min(ca[g], cb[g]) for g in ca)
    return hit / sum(ca.values())


def _notice(topic: str, detail: str, page_no: int, dense: bool = False) -> str:
    """Page 1 is a normal one-page official notice. Later pages are short attachments.

    dense=True fills the page with repeated lines. That layout makes Tesseract
    pathologically slow, so it is timed separately and not used as the base rate.
    """
    if dense:
        line = f"{topic}。{detail}"
        return "\n".join([f"{i}. {line}" for i in range(1, 16)])
    if page_no == 1:
        return "\n".join([
            "各有关单位：",
            f"为做好{topic}工作，现就有关事项通知如下。",
            f"一、工作内容。{detail}",
            "二、时限要求。请于十个工作日内完成自查，并形成问题清单和整改计划。",
            "三、报送方式。书面报送办公室，同步上传台账，不得漏报瞒报。",
            "四、联系方式。联系人值班室。此件供内部工作使用。",
        ])
    return "\n".join([
        f"附件{page_no - 1}  {topic}明细表",
        f"序号{page_no}。责任单位反馈整改进度、责任人和完成时限。",
        detail,
        "备注：本页为附件，分类以首页标题和第一条工作内容为准。",
    ])


def _write_text_page(page, title: str, body: str, fontfile: str) -> None:
    import pymupdf
    page.insert_font(fontname="noto", fontfile=fontfile)
    page.insert_text((64, 78), title, fontname="noto", fontsize=16)
    page.insert_textbox(pymupdf.Rect(64, 108, 530, 800), body, fontname="noto", fontsize=11)


def build_corpus(root: Path) -> list[dict]:
    import pymupdf
    root.mkdir(parents=True, exist_ok=True)
    # (kind, pages). Text set is large enough for a stable extract rate.
    # Scan set stays small because each OCR page costs seconds.
    plan = [("text", n) for n in (
        [1] * 12 + [2] * 8 + [3] * 6 + [5] * 4 + [8] * 3 + [12] * 2 + [20]
    )]
    plan += [("scan", n) for n in (1, 1, 1, 1, 1, 1, 2, 2, 3)]
    plan += [("scan_dense", 1)]
    plan += [("scan_lowdpi", 1), ("scan_lowdpi", 1)]
    meta = []
    for idx, (kind, n_pages) in enumerate(plan):
        title, topic, detail = TOPICS[idx % len(TOPICS)]
        src = pymupdf.open()
        page_text = []
        dense = kind == "scan_dense"
        for pno in range(1, n_pages + 1):
            body = _notice(topic, detail, pno, dense=dense)
            page_text.append(body)
            page = src.new_page(width=595, height=842)
            _write_text_page(page, title if pno == 1 else f"{title}（续）", body, FONT)
            page_text[-1] = page.get_text()
        if kind == "text":
            path = root / f"text_{idx:03d}_{n_pages}p.pdf"
            src.save(path)
            src.close()
            meta.append({"path": str(path), "kind": "text", "pages": n_pages,
                         "title": title, "page_text": page_text})
            continue
        flat = pymupdf.open()
        dpi = 96 if kind == "scan_lowdpi" else 200
        for i in range(n_pages):
            pix = src[i].get_pixmap(dpi=dpi)
            png = pix.tobytes("png")
            page = flat.new_page(width=595, height=842)
            page.insert_image(page.rect, stream=png)
        src.close()
        path = root / f"{kind}_{idx:03d}_{n_pages}p.pdf"
        flat.save(path)
        flat.close()
        meta.append({"path": str(path), "kind": kind, "pages": n_pages,
                     "title": title, "page_text": page_text, "embed_dpi": dpi})
    return meta


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = min(len(ordered) - 1, max(0, int(round((pct / 100) * (len(ordered) - 1)))))
    return ordered[k]


def bench_text(files: list[dict]) -> dict:
    from parser.parser import detect_only, parse_pdf
    detect_s, parse_s = [], []
    pages = chars = 0
    t0 = time.perf_counter()
    for item in files:
        t = time.perf_counter()
        d = detect_only(item["path"])
        detect_s.append(time.perf_counter() - t)
        t = time.perf_counter()
        r = parse_pdf(item["path"])
        parse_s.append(time.perf_counter() - t)
        assert r.ok, r.error
        pages += r.page_count
        chars += sum(len(p.text or "") for p in r.pages)
    elapsed = time.perf_counter() - t0
    parse_only = sum(parse_s) or 1e-6
    return {"files": len(files), "pages": pages, "chars": chars,
            "seconds": round(elapsed, 3),
            "pages_per_sec_single_process": round(pages / parse_only, 2),
            "files_per_sec_single_process": round(len(files) / parse_only, 2),
            "detect_plus_parse_seconds": round(elapsed, 3),
            "detect_ms_p50": round(_percentile(detect_s, 50) * 1000, 2),
            "parse_ms_p50": round(_percentile(parse_s, 50) * 1000, 2),
            "parse_ms_p95": round(_percentile(parse_s, 95) * 1000, 2)}


def _ocr_png(png_path: str, lang: str = "chi_sim+eng", timeout: float = 45.0) -> tuple[float, str, bool]:
    # One thread per process. Uncapped OpenMP plus several tesseract processes
    # livelocks on this host (four pages did not finish in 45s).
    env = os.environ.copy()
    env["OMP_NUM_THREADS"] = "1"
    env["OMP_THREAD_LIMIT"] = "1"
    t = time.perf_counter()
    try:
        proc = subprocess.run(
            ["tesseract", png_path, "stdout", "-l", lang, "--psm", "6"],
            capture_output=True, check=False, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return timeout, "", True
    dt = time.perf_counter() - t
    return dt, proc.stdout.decode("utf-8", "replace"), False


def _render(pdf_path: str, page_index: int, dpi: int, dest: str) -> float:
    import pymupdf
    t = time.perf_counter()
    doc = pymupdf.open(pdf_path)
    try:
        pix = doc[page_index].get_pixmap(dpi=dpi)
        pix.save(dest)
    finally:
        doc.close()
    return time.perf_counter() - t


def bench_ocr(files: list[dict], work: Path, dpi: int, workers: int, timeout: float = 45.0) -> dict:
    work.mkdir(parents=True, exist_ok=True)
    jobs = []
    for item in files:
        for pidx in range(item["pages"]):
            jobs.append((item, pidx, work / f"{Path(item['path']).stem}_p{pidx}_d{dpi}.png"))
    # Warm the language data once; excluded from the rate.
    first = jobs[0]
    _render(first[0]["path"], first[1], dpi, str(first[2]))
    _ocr_png(str(first[2]), timeout=min(20.0, timeout))

    render_s, ocr_s, recalls, title_hits = [], [], [], []
    t0 = time.perf_counter()
    rendered = []
    for item, pidx, dest in jobs:
        render_s.append(_render(item["path"], pidx, dpi, str(dest)))
        rendered.append((item, pidx, dest))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_ocr_png, str(dest), "chi_sim+eng", timeout): (item, pidx)
                for item, pidx, dest in rendered}
        ocr_text = {}
        timed_out = 0
        for fut in as_completed(futs):
            item, pidx = futs[fut]
            dt, text, timeout_hit = fut.result()
            ocr_s.append(dt)
            timed_out += int(timeout_hit)
            ocr_text[(item["path"], pidx)] = (dt, text)
    elapsed = time.perf_counter() - t0
    # Early stop: OCR pages in order until 400 characters or 2 pages.
    key_pages = []
    key_seconds = []
    for item in files:
        got = 0
        spent = 0.0
        used = 0
        for pidx in range(item["pages"]):
            dt, text = ocr_text[(item["path"], pidx)]
            spent += dt
            used += 1
            got += len(_norm(text))
            if pidx == 0:
                title_hits.append(1.0 if _norm(item["title"][:8]) in _norm(text) else 0.0)
                recalls.append(_bigram_recall(item["page_text"][0], text))
            if got >= 400 or used >= 2:
                break
        key_pages.append(used)
        key_seconds.append(spent)
    pages = len(jobs)
    return {"dpi": dpi, "workers": workers, "pages": pages,
            "seconds_render_plus_ocr": round(elapsed, 3),
            "pages_per_sec": round(pages / elapsed, 3),
            "render_ms_p50": round(_percentile(render_s, 50) * 1000, 1),
            "ocr_ms_p50": round(_percentile(ocr_s, 50) * 1000, 1),
            "ocr_ms_p95": round(_percentile(ocr_s, 95) * 1000, 1),
            "ocr_sec_per_page_median": round(statistics.median(ocr_s), 3),
            "per_core_page_sec_render_plus_ocr": round(statistics.median(render_s) + statistics.median(ocr_s), 3),
            "per_core_ocr_pages_per_sec": round(1.0 / (statistics.median(render_s) + statistics.median(ocr_s)), 3),
            "ocr_timeouts": timed_out,
            "title_hit_rate": round(sum(title_hits) / len(title_hits), 3),
            "page1_bigram_recall_mean": round(sum(recalls) / len(recalls), 3),
            "key_ocr_pages_mean": round(sum(key_pages) / len(key_pages), 3),
            "key_ocr_sec_per_file_median": round(statistics.median(key_seconds), 3),
            "chars_page1_source_mean": round(statistics.mean(len(_norm(f["page_text"][0])) for f in files), 1)}


def bench_label(files: list[dict]) -> dict:
    from parser.parser import parse_pdf
    from classifier.backends import KeywordEmbedding, KeywordReranker, ScriptedLLM
    from classifier.config import Thresholds
    from classifier.pipeline import ClassificationPipeline
    llm = ScriptedLLM(default_choice=None)
    pipe = ClassificationPipeline(
        KeywordEmbedding(), KeywordReranker(), llm,
        Thresholds(calibrated=True),
        taxonomy_path=str(ROOT / "contracts" / "taxonomy.yaml"))
    # Representative mix: all text files would dwarf scan cost. Label every
    # parsed text file plus every scan file's text-layer twin is unnecessary;
    # label the text parses, which is the same evidence-router code path.
    times = []
    query_lens = []
    kinds = Counter()
    t0 = time.perf_counter()
    for item in files:
        parsed = parse_pdf(item["path"]).to_dict()
        t = time.perf_counter()
        out = pipe.run(parsed)
        times.append(time.perf_counter() - t)
        kinds[out.get("result_type") or "?"] += 1
        basis = out.get("classification_basis") or {}
        query_lens.append(len(json.dumps(basis, ensure_ascii=False)))
    elapsed = time.perf_counter() - t0
    return {"files": len(files), "seconds": round(elapsed, 3),
            "files_per_sec": round(len(files) / max(1e-6, sum(times)), 2),
            "label_ms_p50": round(_percentile(times, 50) * 1000, 2),
            "label_ms_p95": round(_percentile(times, 95) * 1000, 2),
            "evidence_json_chars_p50": int(_percentile(query_lens, 50)),
            "llm_calls": len(llm.calls),
            "result_types": dict(kinds),
            "wall_seconds_including_reparse": round(elapsed, 3)}


def project(ocr: dict, text: dict, label: dict) -> dict:
    from scheduler.benchmark import concurrent_call_days, route_work, shared_cpu_days, wall_days
    ocr_rate = ocr["per_core_ocr_pages_per_sec"]
    # parse_pdf is single-process. Use that measured rate as one core.
    text_rate = text["pages_per_sec_single_process"]
    label_sec = (label["label_ms_p50"] / 1000.0) or 0.001
    key_pages = ocr["key_ocr_pages_mean"]
    scenarios = [
        ("full_ocr_every_page", 1.0, 10.0, 0.0, 1.0, 10.0, (4, 32, 64, 96)),
        ("route_then_full_ocr_scan", ASSUMED_SCAN_RATIO, 10.0, 10.0, 1.0, 10.0, (4, 64)),
        ("route_full_ocr_dirty_x3", ASSUMED_SCAN_RATIO, 10.0, 10.0, 3.0, 10.0, (64, 96)),
        ("key_content_then_label", ASSUMED_SCAN_RATIO, key_pages, 2.0, 1.0, 10.0, (4, 16, 32, 64)),
        ("key_content_dirty_x3", ASSUMED_SCAN_RATIO, key_pages, 2.0, 3.0, 10.0, (16, 32, 64)),
        ("key_content_dirty_x5", ASSUMED_SCAN_RATIO, key_pages, 2.0, 5.0, 10.0, (32, 64)),
        ("key_content_scan60_dirty_x3", 0.60, key_pages, 2.0, 3.0, 10.0, (32, 64)),
        ("key_content_avg5", ASSUMED_SCAN_RATIO, key_pages, 2.0, 1.0, 5.0, (64,)),
        ("key_content_avg20_dirty_x3", ASSUMED_SCAN_RATIO, key_pages, 2.0, 3.0, 20.0, (64,)),
        ("full_ocr_avg20_dirty_x3", 1.0, 20.0, 0.0, 3.0, 20.0, (64, 96)),
    ]
    rows = []
    for name, scan_ratio, ocr_per, text_per, slowdown, avg_pages, core_list in scenarios:
        for cores in core_list:
            work = route_work(TOTAL_FILES, avg_pages, scan_ratio, ocr_per, text_per)
            stages = []
            if work["text_pages"]:
                stages.append((work["text_pages"], text_rate))
            if work["ocr_pages"]:
                stages.append((work["ocr_pages"], ocr_rate / slowdown))
            cpu_days = shared_cpu_days(stages, cores=cores, utilization=0.72) if stages else 0.0
            label_cpu_days = wall_days(work["label_files"], 1.0 / label_sec, cores, 0.72)
            rows.append({
                "strategy": name,
                "avg_pages": avg_pages,
                "scan_ratio": scan_ratio,
                "ocr_slowdown_vs_clean_sample": slowdown,
                "cores": cores,
                "ocr_pages": int(work["ocr_pages"]),
                "text_pages": int(work["text_pages"]),
                "cpu_extract_days": round(cpu_days, 2),
                "cpu_label_days_measured_classifier": round(label_cpu_days, 2),
                "extract_meets_90d": cpu_days <= DAYS,
            })
    model_rows = []
    for seconds_each, concurrency, note in (
        (0.05, 1, "batched embedding, 50ms, 1 GPU stream"),
        (0.5, 8, "short LLM, 0.5s, 8 concurrent"),
        (2.0, 8, "short LLM, 2s, 8 concurrent"),
        (2.0, 32, "short LLM, 2s, 32 concurrent"),
        (8.0, 8, "long prompt LLM, 8s, 8 concurrent"),
        (8.0, 32, "long prompt LLM, 8s, 32 concurrent"),
        (30.0, 16, "full-document LLM, 30s, 16 concurrent"),
    ):
        days = concurrent_call_days(TOTAL_FILES, seconds_each, concurrency)
        model_rows.append({"note": note, "seconds_each": seconds_each,
                           "concurrency": concurrency, "days": round(days, 2),
                           "meets_90d": days <= DAYS})
    return {"total_files": TOTAL_FILES, "days": DAYS,
            "assumed_scan_ratio": ASSUMED_SCAN_RATIO,
            "required_files_per_sec": round(TOTAL_FILES / (DAYS * 86400), 3),
            "extract_rows": rows, "label_rows": model_rows}


def main() -> int:
    import pymupdf
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="/tmp/capacity_probe.json")
    parser.add_argument("--work", default="/tmp/capacity_probe_samples")
    parser.add_argument("--ocr-workers", type=int, default=min(4, os.cpu_count() or 1))
    args = parser.parse_args()
    work = Path(args.work)
    if not Path(FONT).exists():
        raise SystemExit(f"missing CJK font: {FONT}")
    t0 = time.perf_counter()
    corpus = build_corpus(work)
    text_files = [c for c in corpus if c["kind"] == "text"]
    scan_files = [c for c in corpus if c["kind"] == "scan"]
    low_files = [c for c in corpus if c["kind"] == "scan_lowdpi"]
    text = bench_text(text_files)
    dense_files = [c for c in corpus if c["kind"] == "scan_dense"]
    # Single core first, so the page rate is not inflated by 4-way contention.
    ocr150 = bench_ocr(scan_files, work / "png150", dpi=150, workers=1)
    ocr_parallel = bench_ocr(scan_files[:4], work / "png150p", dpi=150, workers=args.ocr_workers)
    ocr200 = bench_ocr(scan_files[:2], work / "png200", dpi=200, workers=1)
    low = bench_ocr(low_files, work / "pnglow", dpi=150, workers=1)
    dense = bench_ocr(dense_files, work / "pngdense", dpi=150, workers=1, timeout=30)
    label = bench_label(text_files[:40])
    report = {
        "host": {"cpus": os.cpu_count(), "no_gpu": True,
                 "ocr_engine": "tesseract 5 chi_sim+eng",
                 "pdf_engine": f"pymupdf {pymupdf.__doc__.splitlines()[0] if pymupdf.__doc__ else ''}"},
        "sample": {"text_files": len(text_files),
                   "text_pages": sum(f["pages"] for f in text_files),
                   "scan_files": len(scan_files),
                   "scan_pages": sum(f["pages"] for f in scan_files),
                   "lowdpi_files": len(low_files)},
        "text_extract": text,
        "ocr_150dpi_single_core": ocr150,
        "ocr_150dpi_parallel": ocr_parallel,
        "ocr_200dpi_single": ocr200,
        "ocr_lowdpi_source": low,
        "ocr_dense_full_page": dense,
        "label_cpu_classifier": label,
        "projection": project(ocr150, text, label),
        "probe_seconds": round(time.perf_counter() - t0, 1),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "text_pages_per_sec": text["pages_per_sec_single_process"],
        "ocr_150_pages_per_sec": ocr150["pages_per_sec"],
        "ocr_sec_per_page_p50": ocr150["ocr_sec_per_page_median"],
        "ocr_per_core_pages_per_sec": ocr150["per_core_ocr_pages_per_sec"],
        "ocr_parallel_pages_per_sec": ocr_parallel["pages_per_sec"],
        "ocr_200_sec_p50": ocr200["ocr_sec_per_page_median"],
        "dense_sec_p50": dense["ocr_sec_per_page_median"],
        "dense_timeouts": dense["ocr_timeouts"],
        "title_hit_rate": ocr150["title_hit_rate"],
        "recall": ocr150["page1_bigram_recall_mean"],
        "key_pages": ocr150["key_ocr_pages_mean"],
        "label_ms_p50": label["label_ms_p50"],
        "label_types": label["result_types"],
        "out": str(out),
        "probe_seconds": report["probe_seconds"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
