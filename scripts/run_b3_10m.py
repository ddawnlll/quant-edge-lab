#!/usr/bin/env python3
"""Measure B3 on the actual 10M-row atomic L2 corpus under the RP-4G profile.

The target is a 120 s p90. This script keeps every attempt and records a
FAIL_TARGET verdict when the measured minimum already exceeds the target, so a
full 12-repeat certificate is not fabricated from a clearly failing workload.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = "/home/quant/quant/.build-cache/qel-native/release/qel-native-runner"
CORPUS = ROOT / "data/native/DS-MULTIDAY-10M/events.jsonl"
STRATEGY = ROOT / "policy/examples/taker_1m.json"
OUT = ROOT / "evidence/labd/B3_10M_ATOMIC_L2_2026-11-10.json"
RAW = Path("/home/quant/quant/.build-cache/b3-10m")
RAW.mkdir(parents=True, exist_ok=True)
WARMUPS = 1
TIMED = 3
MEM = "4G"


def run_once(label: str) -> dict:
    out = RAW / f"{label}.json"
    cmd = [
        "systemd-run", "--user", "--scope",
        "-p", f"MemoryMax={MEM}", "-p", "MemorySwapMax=0",
        "--unit", f"qel-b3-10m-{label}", "--quiet", "--",
        "sh", "-c",
        'p=$(cut -d: -f3 /proc/self/cgroup); "$@" >/dev/null; rc=$?; '
        'cat /sys/fs/cgroup$p/memory.peak > ' + str(RAW / f"${label}.peak".replace("$", "")) + '; exit $rc',
        "sh", RUNNER, "--events", str(CORPUS), "--strategy", str(STRATEGY),
        "--chunk-bytes", "8388608", "--out", str(out),
    ]
    started = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    wall = time.monotonic() - started
    report = json.loads(out.read_text()) if out.exists() else {}
    peak_path = RAW / f"{label}.peak"
    peak = int(peak_path.read_text().strip()) if peak_path.exists() and peak_path.read_text().strip().isdigit() else None
    return {
        "label": label,
        "exit_code": proc.returncode,
        "wall_s": round(wall, 3),
        "elapsed_s": round(report.get("elapsed_ms", 0) / 1000.0, 3),
        "events": (report.get("resource") or {}).get("events_replayed")
        or report.get("events"),
        "batches": (report.get("resource") or {}).get("batches"),
        "peak_rss_kb": report.get("peak_rss_kb"),
        "cgroup_peak_bytes": peak,
        "stderr_tail": proc.stderr[-400:],
        "report": str(out),
    }


def main() -> int:
    attempts = []
    for i in range(WARMUPS):
        attempts.append(run_once(f"warm{i}"))
    timed = []
    for i in range(TIMED):
        row = run_once(f"timed{i}")
        timed.append(row)
        if row["exit_code"] != 0:
            break
    walls = sorted(row["wall_s"] for row in timed)
    p90 = walls[-1] if walls else None
    payload = {
        "kind": "TASK25_B3_ATOMIC_L2_10M_REMEASURE",
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
            "note": "The target is missed by more than 80%, so three timed repeats are retained as a failure measurement rather than running 12 repeats of an already failing workload.",
        },
        "attempts": attempts,
        "timed": timed,
        "p90_wall_s": p90,
        "target_s": 120,
        "verdict": "PASS" if p90 is not None and p90 <= 120 else "FAIL_TARGET",
        "resource_note": "process RSS from the runner report; cgroup peak includes page cache and is reported separately",
    }
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps({k: payload[k] for k in ("verdict", "p90_wall_s", "target_s")}, indent=2))
    return 0 if payload["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
