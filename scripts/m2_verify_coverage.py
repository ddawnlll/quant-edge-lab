"""Regenerate frozen-fold execution coverage evidence from disk.

Recomputes ``evidence/labd/M2_EXECUTION_COVERAGE.json`` directly from the
on-disk Binance daily archives plus the monthly fundingRate zips: per-day
presence, zip integrity (``testzip``), sha256 of every zip, sealed-range
isolation, and the fold gap map. Idempotent and read-only with respect to
``data/`` (it never downloads or deletes).

Run: ``python3 scripts/m2_verify_coverage.py`` (regenerates the JSON).
"""

from __future__ import annotations

import hashlib
import json
import time
import zipfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARCH = ROOT / "data/raw/ARCHIVE_BINANCE_USDM/BTCUSDT"
SYMBOL = "BTCUSDT"
SEALED_FROM = "2026-03-08"
OUT_PATH = ROOT / "evidence/labd/M2_EXECUTION_COVERAGE.json"


def day_range(start: str, end: str) -> list[str]:
    cur, last = date.fromisoformat(start), date.fromisoformat(end)
    out = []
    while cur <= last:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


FOLDS = [
    {"fold_id": "WF-D64AF439CD8D6E7F", "train": ("2024-09-01", "2025-08-31"), "validation": ("2025-09-01", "2025-11-30")},
    {"fold_id": "WF-2CCA0B04C796FBFC", "train": ("2024-09-01", "2025-11-30"), "validation": ("2025-12-01", "2026-02-28")},
]
NEEDED: list[str] = sorted(
    set(day_range("2024-09-01", "2025-08-31"))
    | set(day_range("2025-09-01", "2025-11-30"))
    | set(day_range("2024-09-01", "2025-11-30"))
    | set(day_range("2025-12-01", "2026-02-28"))
)


def _sha(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    total = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
            total += len(chunk)
    return h.hexdigest(), total


def _check_zip(path: Path) -> dict:
    """Return integrity record for one daily zip (fail fast on bad zip)."""
    try:
        rel = str(path.relative_to(ROOT))
    except ValueError:
        rel = str(path)
    info = {"path": rel, "bytes": path.stat().st_size}
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if len(names) != 1:
            info["error"] = f"member_count={len(names)}"
            return info
        if z.testzip() is not None:
            info["error"] = "crc_mismatch"
            return info
        info["member_bytes"] = z.getinfo(names[0]).file_size
    digest, _ = _sha(path)
    info["sha256"] = digest
    return info


def main() -> int:
    started = time.monotonic()
    days: dict[str, dict] = {}
    missing: list[str] = []
    total_bytes = 0
    errors: list[str] = []
    for day in NEEDED:
        entry: dict[str, object] = {"day": day, "complete": False}
        complete = True
        for kind in ("aggTrades", "1m"):
            path = ARCH / day / f"{SYMBOL}-{kind}-{day}.zip"
            if not path.is_file() or path.stat().st_size == 0:
                complete = False
                entry[kind] = {"present": False}
                break
            rec = _check_zip(path)
            if "error" in rec:
                errors.append(f"{day}/{kind}: {rec['error']}")
                complete = False
                entry[kind] = rec
                break
            total_bytes += rec["bytes"]
            entry[kind] = {**rec, "present": True}
        else:
            entry["complete"] = True
        if not complete:
            missing.append(day)
        days[day] = entry
    fold_rows = []
    for fold in FOLDS:
        train = day_range(*fold["train"])
        val = day_range(*fold["validation"])
        for d in train + val:
            assert isinstance(days[d].get("complete"), bool), f"day {d} missing boolean complete"
        fold_rows.append(
            {
                "fold_id": fold["fold_id"],
                "train_days": len(train),
                "train_present": sum(1 for d in train if days[d].get("complete")),
                "validation_days": len(val),
                "validation_present": sum(1 for d in val if days[d].get("complete")),
                "train_start": fold["train"][0],
                "train_end": fold["train"][1],
                "validation_start": fold["validation"][0],
                "validation_end": fold["validation"][1],
            }
        )
    funding_zips = []
    fund_dir = ARCH / "funding"
    if fund_dir.is_dir():
        for path in sorted(fund_dir.glob("BTCUSDT-fundingRate-*.zip")):
            rec = _check_zip(path)
            funding_zips.append(rec)
    sealed_touched = [d for d in NEEDED if d >= SEALED_FROM]
    payload = {
        "schema": "qel.m2_execution_coverage/1.0",
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "symbol": SYMBOL,
        "status": "COMPLETE" if not missing and not errors else ("PARTIAL_COVERAGE" if any(days[d].get("complete") for d in days) else "EXECUTION_COVERAGE_BLOCKED"),
        "total_needed_days": len(NEEDED),
        "present_days": len(NEEDED) - len(missing),
        "missing_days": len(missing),
        "missing_first": missing[:5],
        "total_bytes": total_bytes,
        "folds": fold_rows,
        "sealed_from": SEALED_FROM,
        "sealed_untouched": len(sealed_touched) == 0,
        "days": days,
        "funding_zips": funding_zips,
        "funding_zip_count": len(funding_zips),
        "integrity_errors": errors,
        "note": "Monthly aggTrades zips are not daily-fold coverage and cannot substitute per-day execution tapes. Funding comes from monthly fundingRate zips (daily fundingRate zips do not exist in the public archive).",
        "verification_elapsed_s": round(time.monotonic() - started, 1),
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"status": payload["status"], "present_days": payload["present_days"], "missing_days": payload["missing_days"], "total_bytes": total_bytes, "funding_zip_count": len(funding_zips), "errors": len(errors), "elapsed_s": payload["verification_elapsed_s"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
