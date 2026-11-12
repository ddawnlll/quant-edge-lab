#!/usr/bin/env python3
"""#94 differential: Mark from 1m trade klines (old) vs markPriceKlines (new).

Development-week evidence only. Never touches the sealed reserve: the caller
passes explicit dev dates and the downloader refuses sealed days.

Usage:
  PYTHONPATH=python .venv/bin/python scripts/mark_source_diff.py \
      --days 2024-09-01 2024-09-02 \
      --out evidence/labd/MARK_SOURCE_DIFF_2026-11-13.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "python"))

import m2_build_fold_tapes_stream as stream  # noqa: E402

ARCH = ROOT / "data" / "raw" / "ARCHIVE_BINANCE_USDM" / "BTCUSDT"
STRATEGY = ROOT / "data" / "native" / "pilot-m2fold-20261111" / "out" / "reports" / "STRAT-VAR-00.strategy.json"
def _runner() -> Path:
    explicit = os.environ.get("QEL_NATIVE_RUNNER")
    candidates = [Path(explicit)] if explicit else []
    cache = Path(os.environ.get("QEL_BUILD_CACHE", "/home/quant/quant/.build-cache"))
    candidates += [
        cache / "qel-native" / "release" / "qel-native-runner",
        ROOT / ".build-cache" / "qel-native" / "release" / "qel-native-runner",
        ROOT / "native" / "runner" / "target" / "release" / "qel-native-runner",
        ROOT / "bin" / "qel-native-runner",
    ]
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return path
    raise SystemExit("qel-native-runner not found; set QEL_NATIVE_RUNNER")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def mark_rows(path: Path) -> dict[int, str]:
    rows: dict[int, str] = {}
    with path.open() as fh:
        for line in fh:
            event = json.loads(line)
            if event["kind"] == "Mark":
                rows[int(event["ts_ns"])] = event["price"]
    return rows


def max_abs_delta(a: dict[int, str], b: dict[int, str]) -> str:
    keys = sorted(set(a) & set(b))
    if not keys:
        return "0"
    worst = max(abs(Decimal(a[k]) - Decimal(b[k])) for k in keys)
    return str(worst)


def build(day: str, out_dir: Path, *, mark_path: Path, source: str) -> dict:
    stream.ARCH = ARCH
    stream.OUT = out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    funding = stream.funding_for_day(stream.load_funding_rows({day}), day)
    return stream.build_day(day, funding, mark_source=source, mark_path=mark_path)


def replay(tape: Path, out: Path) -> dict:
    if out.is_file():
        out.unlink()
    subprocess.run(
        [str(_runner()), "--events", str(tape), "--strategy", str(STRATEGY), "--out", str(out)],
        check=True,
        cwd=str(ROOT),
    )
    return json.loads(out.read_text())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", nargs="+", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--work", default="/tmp/qel-mark-source-diff")
    args = ap.parse_args()

    work = Path(args.work)
    if work.exists():
        shutil.rmtree(work)
    old_dir, new_dir = work / "old", work / "new"
    results = []
    for day in args.days:
        trade_mark = ARCH / day / f"BTCUSDT-1m-{day}.zip"
        real_mark = ARCH / day / f"BTCUSDT-markPriceKlines-1m-{day}.zip"
        for required in (trade_mark, real_mark):
            if not required.is_file():
                raise SystemExit(f"missing input: {required} (fetch with scripts/m2_ingest_full.py --mark-only)")

        old_build = build(day, old_dir, mark_path=trade_mark, source="trade_kline_close")
        new_build = build(day, new_dir, mark_path=real_mark, source="markPriceKlines")
        old_tape = old_dir / f"{day}.jsonl"
        new_tape = new_dir / f"{day}.jsonl"

        old_marks = mark_rows(old_tape)
        new_marks = mark_rows(new_tape)
        old_replay = replay(old_tape, work / f"{day}-old.json")
        new_replay = replay(new_tape, work / f"{day}-new.json")

        def summary(build_info: dict, replay_info: dict) -> dict:
            account = replay_info.get("native_account", {})
            funding = replay_info.get("funding", {})
            return {
                "mark_rows": build_info["marks"],
                "mark_label": build_info["mark_label"],
                "funding_count": funding.get("count"),
                "funding_total": funding.get("total"),
                "final_equity": account.get("equity"),
                "free_cash": account.get("free"),
                "unrealized": account.get("unrealized"),
                "positions": replay_info.get("total_positions"),
                "orders": replay_info.get("total_orders"),
                "tape_entries": len(replay_info.get("tape", [])),
            }

        results.append(
            {
                "day": day,
                "inputs": {
                    "trade_klines_sha256": sha256(trade_mark),
                    "mark_price_klines_sha256": sha256(real_mark),
                },
                "old_trade_close_proxy": summary(old_build, old_replay),
                "new_mark_price_klines": summary(new_build, new_replay),
                "delta": {
                    "max_abs_mark_change": max_abs_delta(old_marks, new_marks),
                    "equity_change": str(
                        Decimal(str(new_replay.get("native_account", {}).get("equity", "0")))
                        - Decimal(str(old_replay.get("native_account", {}).get("equity", "0")))
                    ),
                    "tape_entries_changed": sum(
                        1 for k in set(old_marks) | set(new_marks) if old_marks.get(k) != new_marks.get(k)
                    ),
                },
            }
        )

    payload = {
        "schema": "qel.mark_source_diff/1.0",
        "issue": 94,
        "generated_by": "scripts/mark_source_diff.py",
        "scope": "development week only; no sealed/OOS data; no profitability claim",
        "days": results,
        "limits": [
            "Only the development week was re-run.",
            "Same strategy (STRAT-VAR-00) and runner for both tapes.",
            "Equity differences are mark-source effects on this strategy, not an edge.",
        ],
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
