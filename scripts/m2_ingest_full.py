"""Full-window execution ingest (night run).

Downloads every missing daily BTCUSDT aggTrades+1m zip for the frozen TASK-12
folds (546 days), skipping existing non-empty files. Conservative concurrency,
resumable per-day, content-hash manifest. Sealed reserve (from 2026-03-08)
is never touched. Bounded by available disk.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.request
from datetime import date, timedelta
from pathlib import Path

ARCHIVE_ROOT = Path("data/raw/ARCHIVE_BINANCE_USDM/BTCUSDT")
BASE = "https://data.binance.vision/data/futures/um/daily"
SYMBOL = "BTCUSDT"
SEALED_FROM = "2026-03-08"
MANIFEST_PATH = Path("evidence/labd/M2_EXECUTION_COVERAGE.json")


def day_range(start: str, end: str) -> list[str]:
    cur, last = date.fromisoformat(start), date.fromisoformat(end)
    out = []
    while cur <= last:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


NEEDED: list[str] = sorted(
    set(day_range("2024-09-01", "2025-08-31"))
    | set(day_range("2025-09-01", "2025-11-30"))
    | set(day_range("2024-09-01", "2025-11-30"))
    | set(day_range("2025-12-01", "2026-02-28"))
)


def present_full(day: str) -> bool:
    d = ARCHIVE_ROOT / day
    return (d / f"{SYMBOL}-aggTrades-{day}.zip").is_file() and (d / f"{SYMBOL}-1m-{day}.zip").is_file()


def fetch_one(day: str, kind: str) -> tuple[str, int]:
    if day >= SEALED_FROM:
        raise ValueError(f"SEALED_DAY_REFUSED day={day} sealed_from={SEALED_FROM}")
    dest = ARCHIVE_ROOT / day
    dest.mkdir(parents=True, exist_ok=True)
    name = f"{SYMBOL}-{kind}-{day}.zip"
    out = dest / name
    if out.is_file() and out.stat().st_size > 0:
        return ("skip", out.stat().st_size)
    url = (
        f"{BASE}/aggTrades/{SYMBOL}/{name}"
        if kind == "aggTrades"
        else f"{BASE}/klines/{SYMBOL}/1m/{name}"
    )
    tmp = out.with_suffix(".part")
    for attempt in range(3):
        try:
            urllib.request.urlretrieve(url, tmp)
            break
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
    tmp.rename(out)
    return ("ok", out.stat().st_size)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_mark_one(day: str) -> tuple[str, int]:
    """Fetch real ``markPriceKlines`` for one day, verifying the official CHECKSUM.

    The mark file is stored under a distinct name. Binance names both the trade
    klines and the mark klines ``<SYM>-1m-<date>.zip``; using the same local name
    for both is exactly how #94 happened.
    """
    if day >= SEALED_FROM:
        raise ValueError(f"SEALED_DAY_REFUSED day={day} sealed_from={SEALED_FROM}")
    dest = ARCHIVE_ROOT / day
    dest.mkdir(parents=True, exist_ok=True)
    name = f"{SYMBOL}-markPriceKlines-1m-{day}.zip"
    out = dest / name
    if out.is_file() and out.stat().st_size > 0:
        return ("skip", out.stat().st_size)
    url = f"{BASE}/markPriceKlines/{SYMBOL}/1m/{SYMBOL}-1m-{day}.zip"
    tmp = out.parent / (out.name + ".part")
    sum_tmp = out.parent / (out.name + ".part.CHECKSUM")
    for attempt in range(3):
        try:
            urllib.request.urlretrieve(url, tmp)
            urllib.request.urlretrieve(url + ".CHECKSUM", sum_tmp)
            break
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
    want = sum_tmp.read_text().split()[0]
    got = sha256_file(tmp)
    sum_tmp.unlink()
    if got != want:
        tmp.unlink()
        raise ValueError(f"MARK_CHECKSUM_MISMATCH day={day} want={want} got={got}")
    tmp.rename(out)
    return ("ok", out.stat().st_size)


def main() -> int:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    missing = [d for d in NEEDED if d < SEALED_FROM and not present_full(d)]
    print(f"needed={len(NEEDED)} missing={len(missing)}", flush=True)
    fetched: list[dict] = []
    failed: list[str] = []
    # Conservative: 4 workers, official public archive. Two jobs per day sequentially.
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_fetch_day, d): d for d in missing}
        for fut in as_completed(futures):
            day = futures[fut]
            try:
                entry = fut.result()
                fetched.append(entry)
                print(f"OK {day} agg={entry['aggTrades']['bytes']} 1m={entry['1m']['bytes']}", flush=True)
            except Exception as exc:
                failed.append(day)
                print(f"FAIL {day}: {exc}", flush=True)
    # Manifest update
    report = json.loads(MANIFEST_PATH.read_text()) if MANIFEST_PATH.exists() else {}
    report["ingest_complete_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    report["fetched_days"] = sorted(f["day"] for f in fetched)
    report["fetched_count"] = len(fetched)
    # Content-hash manifest: record the per-day sha256/bytes already computed
    # by _fetch_day under ingest_hashes (never overwriting the verifier's
    # per-day "days" structure in the same file).
    ingest_hashes = dict(report.get("ingest_hashes") or {})
    for f in fetched:
        ingest_hashes[f["day"]] = {"aggTrades": f["aggTrades"], "1m": f["1m"]}
    report["ingest_hashes"] = ingest_hashes
    report["failed_days"] = sorted(failed)
    # recompute coverage
    present_days = sum(1 for d in NEEDED if d < SEALED_FROM and present_full(d))
    report["present_days"] = present_days
    report["missing_days"] = len([d for d in NEEDED if d < SEALED_FROM and not present_full(d)])
    report["status"] = "COMPLETE" if report["missing_days"] == 0 else "PARTIAL_COVERAGE"
    MANIFEST_PATH.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"DONE present={present_days} missing={report['missing_days']} status={report['status']}", flush=True)
    return 1 if failed else 0


def _fetch_day(day: str) -> dict:
    _, agg_bytes = fetch_one(day, "aggTrades")
    _, m_bytes = fetch_one(day, "1m")
    d = ARCHIVE_ROOT / day
    return {
        "day": day,
        "aggTrades": {
            "sha256": sha256_file(d / f"{SYMBOL}-aggTrades-{day}.zip"),
            "bytes": agg_bytes,
        },
        "1m": {
            "sha256": sha256_file(d / f"{SYMBOL}-1m-{day}.zip"),
            "bytes": m_bytes,
        },
    }


if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 4 and sys.argv[1] == "--mark-only":
        for day in day_range(sys.argv[2], sys.argv[3]):
            print(day, fetch_mark_one(day), flush=True)
        raise SystemExit(0)
    raise SystemExit(main())
