from __future__ import annotations
import time
from dataclasses import dataclass


@dataclass
class Sample:
    n_files: int
    n_pages: int
    seconds: float

    @property
    def pages_per_sec(self) -> float:
        return self.n_pages / max(1e-6, self.seconds)


def estimate_full_run(sample: Sample, total_files: int, avg_pages: float = 10.0,
                      utilization: float = 0.72) -> dict:
    total_pages = total_files * avg_pages
    eff = sample.pages_per_sec * utilization
    secs = total_pages / max(1e-6, eff)
    return {"sample_pages_per_sec": round(sample.pages_per_sec, 2),
            "effective_pages_per_sec": round(eff, 2),
            "total_pages": int(total_pages),
            "est_days": round(secs / 86400, 1),
            "meets_90d": secs <= 90 * 86400}


def route_work(total_files: int, avg_pages: float, scan_ratio: float,
               ocr_pages_per_scan: float, text_pages_per_text: float) -> dict:
    """Split a corpus into OCR pages, text-extract pages, and one label per file.

    scan_ratio is the share of files with no usable text layer (scan + image).
    Page caps are applied per file and never exceed avg_pages.
    """
    if total_files < 0 or avg_pages < 0:
        raise ValueError("counts must be non-negative")
    if not 0.0 <= scan_ratio <= 1.0:
        raise ValueError("scan_ratio must be between 0 and 1")
    ocr_per = min(float(avg_pages), float(ocr_pages_per_scan))
    text_per = min(float(avg_pages), float(text_pages_per_text))
    scan_files = total_files * scan_ratio
    text_files = total_files * (1.0 - scan_ratio)
    return {"scan_files": scan_files,
            "text_files": text_files,
            "ocr_pages": scan_files * ocr_per,
            "text_pages": text_files * text_per,
            "label_files": float(total_files)}


def wall_days(units: float, units_per_sec_per_worker: float, workers: float,
              utilization: float = 0.72) -> float:
    """Calendar days for one stage. Workers share that stage only."""
    if units < 0:
        raise ValueError("units must be non-negative")
    if units_per_sec_per_worker <= 0 or workers <= 0 or utilization <= 0:
        raise ValueError("rate, workers, and utilization must be positive")
    return units / (units_per_sec_per_worker * workers * utilization) / 86400.0


def shared_cpu_days(stages: list[tuple[float, float]], cores: float,
                    utilization: float = 0.72) -> float:
    """Days when stages share one CPU pool and do not overlap.

    stages is (unit_count, units_per_sec_per_core). This is the conservative
    bound: text extraction, raster, and CPU OCR take turns on the same cores.
    """
    if cores <= 0 or utilization <= 0:
        raise ValueError("cores and utilization must be positive")
    core_seconds = 0.0
    for units, rate in stages:
        if units < 0 or rate <= 0:
            raise ValueError("stage units and rate must be positive")
        core_seconds += units / rate
    return core_seconds / (cores * utilization) / 86400.0


def concurrent_call_days(n_items: float, seconds_each: float, concurrency: float) -> float:
    """Days to finish n_items calls of latency seconds_each at a fixed concurrency."""
    if n_items < 0 or seconds_each < 0:
        raise ValueError("counts must be non-negative")
    if concurrency <= 0:
        raise ValueError("concurrency must be positive")
    return n_items * seconds_each / concurrency / 86400.0
