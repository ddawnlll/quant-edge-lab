#!/usr/bin/env python3
"""Compare legacy NDJSON and Parquet Nautilus replay outputs.

Usage:
  PYTHONPATH=python .venv/bin/python scripts/compare_replay_outputs.py \
    --legacy <legacy.json> --parquet <parquet.json> [--out <report.json>]

Compares event-by-event tape entries plus accounting outputs:
  * tape (order, timestamps, prices, quantities, fees, funding)
  * native_account (cash/equity/positions/mark)
  * funding totals, order/position totals, iterations
  * resource counts (batches, events replayed, purges)

Volatile fields are ignored: elapsed_ms, peak_rss_kb, events_path,
rss_samples (timing/memory differ by construction). The first tape
divergence (index and both entries) is reported if parity fails.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

VOLATILE = {"elapsed_ms", "peak_rss_kb", "events_path"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--legacy", required=True)
    ap.add_argument("--parquet", required=True)
    ap.add_argument("--out")
    args = ap.parse_args()

    legacy = json.loads(Path(args.legacy).read_text())
    parquet = json.loads(Path(args.parquet).read_text())

    tape_a = legacy.get("tape", [])
    tape_b = parquet.get("tape", [])
    first_divergence = None
    if tape_a != tape_b:
        n = min(len(tape_a), len(tape_b))
        idx = next((i for i in range(n) if tape_a[i] != tape_b[i]), n)
        first_divergence = {
            "index": idx,
            "legacy": tape_a[idx] if idx < len(tape_a) else None,
            "parquet": tape_b[idx] if idx < len(tape_b) else None,
        }

    def strip(d: dict) -> dict:
        return {k: v for k, v in d.items() if k not in VOLATILE and k != "tape"}

    # Compare resource blocks ignoring rss samples (timing-dependent).
    res_a = dict(legacy.get("resource", {}))
    res_b = dict(parquet.get("resource", {}))
    res_a.pop("rss_samples", None)
    res_b.pop("rss_samples", None)
    res_a.pop("rss_samples_path", None)
    res_b.pop("rss_samples_path", None)

    checks = {
        "tape_equal": tape_a == tape_b,
        "tape_len": [len(tape_a), len(tape_b)],
        "native_account_equal": legacy.get("native_account") == parquet.get("native_account"),
        "funding_equal": legacy.get("funding") == parquet.get("funding"),
        "status_equal": legacy.get("status") == parquet.get("status"),
        "events_equal": legacy.get("events") == parquet.get("events"),
        "iterations_equal": legacy.get("iterations") == parquet.get("iterations"),
        "total_orders_equal": legacy.get("total_orders") == parquet.get("total_orders"),
        "total_positions_equal": legacy.get("total_positions") == parquet.get("total_positions"),
        "engine_totals_equal": (
            legacy.get("engine_total_orders") == parquet.get("engine_total_orders")
            and legacy.get("engine_total_positions") == parquet.get("engine_total_positions")
        ),
        "resource_counts_equal": res_a == res_b,
        "residual": {
            "legacy_native_account": legacy.get("native_account"),
            "parquet_native_account": parquet.get("native_account"),
            "legacy_funding": legacy.get("funding"),
            "parquet_funding": parquet.get("funding"),
        },
    }
    verdict = "PASS" if all(
        v for k, v in checks.items() if k not in ("tape_len", "residual")
    ) else "FAIL"
    report = {
        "legacy": args.legacy,
        "parquet": args.parquet,
        "checks": checks,
        "first_divergence": first_divergence,
        "legacy_stripped": strip(legacy),
        "parquet_stripped": strip(parquet),
        "resource_legacy": res_a,
        "resource_parquet": res_b,
        "verdict": verdict,
    }
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.out:
        Path(args.out).write_text(text)
    print(text)
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
