#!/usr/bin/env python3
"""Recheck B2's 30 s warm p90 on the frozen quote/trade corpus under RP-4G."""

from __future__ import annotations

import json
import statistics
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = "/home/quant/quant/.build-cache/qel-native/release/qel-native-runner"
CORPUS = ROOT / "data/native/bench/events_100x.jsonl"
STRATEGY = ROOT / "policy/examples/taker_1s.json"
OUT = ROOT / "evidence/labd/B2_RECHECK_2026-11-10.json"
RAW = Path("/home/quant/quant/.build-cache/b2-recheck")
RAW.mkdir(parents=True, exist_ok=True)
WARMUPS = 2
TIMED = 12
MEM = "4G"


def run_once(label: str) -> dict:
    out = RAW / f"{label}.json"
    peak = RAW / f"{label}.peak"
    inner = (
        'p=$(cut -d: -f3 /proc/self/cgroup); '
        '"$@" >/dev/null; rc=$?; '
        f'cat /sys/fs/cgroup$p/memory.peak > {peak}; exit $rc'
    )
    cmd = [
        "systemd-run", "--user", "--scope",
        "-p", f"MemoryMax={MEM}", "-p", "MemorySwapMax=0",
        "--unit", f"qel-b2-{label}", "--quiet", "--",
        "sh", "-c", inner, "sh", RUNNER,
        "--events", str(CORPUS), "--strategy", str(STRATEGY),
        "--chunk-bytes", "8388608", "--out", str(out),
    ]
    started = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    wall = time.monotonic() - started
    report = json.loads(out.read_text()) if out.exists() else {}
    peak_bytes = int(peak.read_text().strip()) if peak.exists() and peak.read_text().strip().isdigit() else None
    return {
        "label": label,
        "exit_code": proc.returncode,
        "wall_s": round(wall, 3),
        "elapsed_s": round(report.get("elapsed_ms", 0) / 1000.0, 3),
        "events": (report.get("resource") or {}).get("events_replayed")
        or report.get("events"),
        "batches": (report.get("resource") or {}).get("batches"),
        "peak_rss_kb": report.get("peak_rss_kb"),
        "cgroup_peak_bytes": peak_bytes,
        "stderr_tail": proc.stderr[-300:],
        "report": str(out),
    }


def nearest_rank_p90(values: list[float]) -> float:
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(0.9 * len(ordered) + 0.999999) - 1))
    return ordered[idx]


def main() -> int:
    warm = [run_once(f"warm{i}") for i in range(WARMUPS)]
    timed = [run_once(f"timed{i:02d}") for i in range(TIMED)]
    walls = [row["wall_s"] for row in timed]
    p90 = nearest_rank_p90(walls) if walls else None
    payload = {
        "kind": "TASK25_B2_RECHECK",
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "profile": "RP-4G",
        "memory_max_bytes": 4 * 1024**3,
        "memory_swap_max_bytes": 0,
        "workload": {
            "corpus": str(CORPUS),
            "bytes": CORPUS.stat().st_size,
            "strategy": str(STRATEGY),
            "mode": "--chunk-bytes 8388608",
        },
        "protocol": {
            "warmups": WARMUPS,
            "timed_repeats": TIMED,
            "p90_rule": "nearest-rank p90 of the 12 timed wall-clock runs",
        },
        "warmups": warm,
        "timed": timed,
        "median_wall_s": round(statistics.median(walls), 3) if walls else None,
        "p90_wall_s": p90,
        "target_s": 30,
        "verdict": "PASS" if p90 is not None and p90 <= 30 else "FAIL_MARGINAL",
        "resource_note": "process RSS from the runner report; cgroup peak includes page cache and is reported separately",
        "failed_attempts_retained": True,
    }
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps({k: payload[k] for k in ("verdict", "p90_wall_s", "median_wall_s", "target_s")}, indent=2))
    return 0 if payload["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
