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
