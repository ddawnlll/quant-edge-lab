"""A2: walk-forward execution-coverage audit + resumable ingestion (bounded).

Fold windows come from the frozen TASK-12 split manifest
(``evidence/labd/PROTOCOL_MONTHLY_26M.json``):
  Fold 1 WF-D64AF439: train 2024-09-01..2025-08-31, val 2025-09-01..2025-11-30
  Fold 2 WF-2CCA0B04: train 2024-09-01..2025-11-30, val 2025-12-01..2026-02-28
Reserved (sealed) starts 2026-03-08 — ingestion never touches it.

Coverage (2026-11-10 night run): all 546 fold days present on disk after the
full ingest (see ``evidence/labd/M2_EXECUTION_COVERAGE.json`` status
COMPLETE). This script audits coverage, emits a gap map, and fetches an
explicit bounded ``--days N`` pilot. ``--full`` requires ``--confirm``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from datetime import date, timedelta
from pathlib import Path

SCHEMA = "qel.m2_execution_coverage/1.0"
ARCHIVE_ROOT = Path("data/raw/ARCHIVE_BINANCE_USDM/BTCUSDT")
BASE_URL = "https://data.binance.vision/data/futures/um/daily"
SYMBOL = "BTCUSDT"

FOLDS = [
    {"fold_id": "WF-D64AF439CD8D6E7F", "train_start": "2024-09-01", "train_end": "2025-08-31", "validation_start": "2025-09-01", "validation_end": "2025-11-30"},
    {"fold_id": "WF-2CCA0B04C796FBFC", "train_start": "2024-09-01", "train_end": "2025-11-30", "validation_start": "2025-12-01", "validation_end": "2026-02-28"},
]
SEALED_FROM = "2026-03-08"


def day_range(start: str, end: str) -> list[str]:
    cur, last = date.fromisoformat(start), date.fromisoformat(end)
    out = []
    while cur <= last:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def day_present(day: str) -> bool:
    """Daily archive day counts as present iff both aggTrades and 1m zips exist."""
    d = ARCHIVE_ROOT / day
    return (d / f"{SYMBOL}-aggTrades-{day}.zip").exists() and (d / f"{SYMBOL}-1m-{day}.zip").exists()


def audit() -> dict:
    per_fold = []
    all_needed: set[str] = set()
    for fold in FOLDS:
        train = day_range(fold["train_start"], fold["train_end"])
        val = day_range(fold["validation_start"], fold["validation_end"])
        for day in train + val:
            if day < SEALED_FROM:
                all_needed.add(day)
        per_fold.append(
            {
                "fold_id": fold["fold_id"],
                "train_days": len(train),
                "train_present": sum(1 for d in train if day_present(d)),
                "validation_days": len(val),
                "validation_present": sum(1 for d in val if day_present(d)),
                "missing_before_first_available": None,
            }
        )
    needed_sorted = sorted(all_needed)
    missing = [d for d in needed_sorted if not day_present(d)]
    total_needed = len(needed_sorted)
    present = total_needed - len(missing)
    status = (
        "COMPLETE"
        if not missing
        else "EXECUTION_COVERAGE_BLOCKED" if present == 0 else "PARTIAL_COVERAGE"
    )
    est_bytes_full = total_needed * 12_700_000  # observed compressed avg/day
    return {
        "schema": SCHEMA,
        "symbol": SYMBOL,
        "folds": per_fold,
        "total_needed_days": total_needed,
        "present_days": present,
        "missing_days": len(missing),
        "missing_first": missing[:5],
        "status": status,
        "estimated_full_bytes": est_bytes_full,
        "sealed_from": SEALED_FROM,
        "sealed_untouched": True,
        "note": (
            "Monthly aggTrades zips (2024-09, 2026-08..10) are not daily-fold coverage; "
            "they cannot substitute per-day execution tapes. Full window ~9.7GB at 12.7MB/day."
        ),
    }


KIND_DIR = {"aggTrades": "aggTrades", "1m": "klines/BTCUSDT/1m"}


def fetch_day(day: str) -> Path:
    dest = ARCHIVE_ROOT / day
    dest.mkdir(parents=True, exist_ok=True)
    for kind in ("aggTrades", "1m"):
        name = f"{SYMBOL}-{kind}-{day}.zip"
        out = dest / name
        if out.exists() and out.stat().st_size > 0:
            continue
        # aggTrades: .../aggTrades/<SYM>/<name>; klines 1m: .../klines/<SYM>/1m/<name>
        if kind == "1m":
            url = f"{BASE_URL}/klines/{SYMBOL}/1m/{name}"
        else:
            url = f"{BASE_URL}/aggTrades/{SYMBOL}/{name}"
        tmp = out.with_suffix(".part")
        try:
            urllib.request.urlretrieve(url, tmp)
        except urllib.error.HTTPError as exc:
            if tmp.exists():
                tmp.unlink()
            raise RuntimeError(f"ARCHIVE_DAY_MISSING {kind} {day} HTTP {exc.code}") from exc
        tmp.rename(out)
    return dest


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=0, help="bounded pilot: fetch N missing days")
    ap.add_argument("--start-from", default=None, help="pilot start date (default first missing fold day)")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--confirm", action="store_true", help="required for --full")
    ap.add_argument("--out", default="evidence/labd/M2_EXECUTION_COVERAGE.json")
    args = ap.parse_args()
    if args.full and not args.confirm:
        print("REFUSED: --full requires --confirm", file=sys.stderr)
        return 2
    report = audit()
    if args.days or args.full:
        needed = [d for d in day_range(FOLDS[0]["train_start"], FOLDS[1]["validation_end"]) if d < SEALED_FROM and not day_present(d)]
        if args.start_from:
            needed = [d for d in needed if d >= args.start_from]
            if needed:
                idx = needed.index(min(needed))
                needed = needed[idx:]
        if not args.full:
            needed = needed[: args.days]
        fetched = []
        for day in needed:
            dest = fetch_day(day)
            entry = {"day": day}
            for kind in ("aggTrades", "1m"):
                p = dest / f"{SYMBOL}-{kind}-{day}.zip"
                entry[kind] = {"sha256": sha256_file(p), "bytes": p.stat().st_size}
            fetched.append(entry)
            print(f"fetched {day} aggTrades={entry['aggTrades']['bytes']} 1m={entry['1m']['bytes']}")
        report["pilot"] = {"days": len(fetched), "fetched": fetched}
        report["post_audit"] = audit()
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
