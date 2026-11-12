"""Streaming fold-tape builder (A3 prep).

Produces byte-identical NDJSON to ``qel.prepare.archive.build`` for the
given inputs (aggTrades zip, 1m mark zip, funding rows) without materialising
~1M trade rows in memory:

* synthetic L1 quote per second from the last observed trade (same rule),
* quotes and trades emitted in archive order,
* marks and funding merged in (ts, kind) order with a strict ``<`` flush so
  that at equal timestamps quotes/trades come first, then marks, then funding
  (exactly the stable-sort order of the canonical builder, which appends
  trades/quotes, then marks, then funding and sorts by ``ts_ns``).

Funding comes from Binance monthly ``fundingRate`` zips
(``data/raw/ARCHIVE_BINANCE_USDM/BTCUSDT/funding/BTCUSDT-fundingRate-YYYY-MM.zip``,
fetched from the public archive on demand). If a monthly zip is missing and
unreachable the day build fails loudly -- no silent funding gap.

Output: ``data/native/_fold/{day}.jsonl`` (atomic tmp + rename, idempotent).
``data/native/_fold/manifest.json`` records per-day counts and sha256.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import time
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ARCH = Path("data/raw/ARCHIVE_BINANCE_USDM/BTCUSDT")
FUND_DIR = ARCH / "funding"
OUT = Path("data/native/_fold")
BASE = "https://data.binance.vision/data/futures/um/monthly/fundingRate/BTCUSDT"
TICK = Decimal("0.01")
MANIFEST_PATH = OUT / "manifest.json"


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


def _sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _funding_zip(month: str) -> Path:
    path = FUND_DIR / f"BTCUSDT-fundingRate-{month}.zip"
    if path.is_file() and path.stat().st_size > 0:
        return path
    FUND_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    url = f"{BASE}/BTCUSDT-fundingRate-{month}.zip"
    for attempt in range(3):
        try:
            urllib.request.urlretrieve(url, tmp)
            break
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
    tmp.rename(path)
    return path


def load_funding_rows(days: set[str]) -> list[tuple[int, str]]:
    """Load observed funding (calc_time_ms, rate) rows for the given days."""
    months = sorted({d[:7] for d in days})
    rows: list[tuple[int, str]] = []
    for month in months:
        with zipfile.ZipFile(_funding_zip(month)) as z:
            with z.open(z.namelist()[0]) as fh:
                for row in csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8")):
                    rows.append((int(row["calc_time"]), str(Decimal(row["last_funding_rate"]).normalize())))
    rows.sort(key=lambda r: r[0])
    return rows


def funding_for_day(rows: list[tuple[int, str]], day: str) -> list[tuple[int, str]]:
    """Return only the funding rows whose calc_time falls in the UTC day.

    This is the single place where day-filtering happens. Callers (main)
    must pass the result to build_day; build_day itself never filters,
    it only validates the precondition that its input is already exact.
    """
    start_ms = int(datetime.combine(date.fromisoformat(day), datetime.min.time(), timezone.utc).timestamp() * 1000)
    end_ms = start_ms + 86400_000
    return [(ts_ms, rate) for ts_ms, rate in rows if start_ms <= ts_ms < end_ms]


def build_day(day: str, funding_rows: list[tuple[int, str]], *, mark_source: str = "markPriceKlines", mark_path: Path | None = None) -> dict:
    agg = ARCH / day / f"BTCUSDT-aggTrades-{day}.zip"
    mark = mark_path or ARCH / day / f"BTCUSDT-markPriceKlines-1m-{day}.zip"
    out = OUT / f"{day}.jsonl"
    if out.is_file() and out.stat().st_size > 0:
        return {"day": day, "status": "skip"}
    tmp = OUT / f".{day}.jsonl.tmp"
    # Marks are small (1440/day); funding is 3/day. Keep both sorted so the
    # merged flush below matches canonical stable-sort order. This fails
    # closed (MARK_SOURCE_MISSING) if the file is trade klines, not mark data.
    from qel.prepare.archive import MARK_LABEL, MARK_PROXY_LABEL, load_mark_rows

    mark_rows = load_mark_rows(str(mark), source=mark_source)
    mark_label = MARK_LABEL if mark_source == "markPriceKlines" else MARK_PROXY_LABEL
    # Caller precondition (enforced, never silently filtered): funding_rows
    # must already be exactly this UTC day, via funding_for_day in main.
    # Canonical build writes every funding row it is given, so silently
    # dropping out-of-day rows here would diverge from canonical bytes.
    start_ms = int(datetime.combine(date.fromisoformat(day), datetime.min.time(), timezone.utc).timestamp() * 1000)
    end_ms = start_ms + 86400_000
    for ts_ms, _rate in funding_rows:
        if not (start_ms <= ts_ms < end_ms):
            raise ValueError(f"FUNDING_OUT_OF_DAY day={day} ts_ms={ts_ms} window=[{start_ms},{end_ms}) caller must pass funding_for_day(rows, day)")
    fund_rows = list(funding_rows)
    if not fund_rows:
        # Binance USD-M funds 3x/day, so a fold day with no settlement row
        # is a source defect, not a complete day. Fail closed (FUNDING_GAP)
        # rather than returning ok with funding=0 and claiming a full tape.
        raise ValueError(f"FUNDING_GAP day={day} no funding rows in [{start_ms},{end_ms})")

    # Merged (ts_ms, order, payload) for marks and funding; order 0 = mark,
    # 1 = funding so exact ties match canonical (marks appended before funding).
    pending: list[tuple[int, int, dict]] = []
    for ts_ms, price in mark_rows:
        pending.append((ts_ms, 0, {"kind": "Mark", "ts_ns": str(ts_ms * 1_000_000), "price": price}))
    for ts_ms, rate in fund_rows:
        pending.append((ts_ms, 1, {"kind": "Funding", "ts_ns": str(ts_ms * 1_000_000), "rate": rate}))
    pending.sort(key=lambda item: (item[0], item[1]))
    pending_idx = 0

    def flush_before(ts_ms_limit: int, fh) -> None:
        nonlocal pending_idx
        while pending_idx < len(pending) and pending[pending_idx][0] < ts_ms_limit:
            fh.write(json.dumps(pending[pending_idx][2], sort_keys=True) + "\n")
            pending_idx += 1

    za = zipfile.ZipFile(agg)
    sha = hashlib.sha256()
    n_q = n_t = n_m = n_f = 0
    prev_ts_ms: int | None = None
    try:
        with za.open(za.namelist()[0]) as f, open(tmp, "w") as raw:
            fh = _HashWriter(raw, sha)
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"))
            last_second = None
            for row in reader:
                ts_ms = int(row["transact_time"])
                # Canonical build sorts by ts_ns, so a non-monotonic aggTrades
                # CSV would diverge in bytes. Fail closed instead of emitting
                # a differently-ordered tape and claiming byte identity.
                if prev_ts_ms is not None and ts_ms < prev_ts_ms:
                    raise ValueError(f"NON_MONOTONIC_AGGTRADES day={day} ts_ms={ts_ms} prev_ts_ms={prev_ts_ms}")
                prev_ts_ms = ts_ms
                price = Decimal(row["price"])
                bucket = ts_ms // 1000
                if last_second is None or bucket != last_second:
                    last_second = bucket
                    quote_ts_ms = bucket * 1000
                    flush_before(quote_ts_ms, fh)
                    fh.write(
                        json.dumps(
                            {
                                "kind": "Quote",
                                "ts_ns": str(quote_ts_ms * 1_000_000),
                                "bid": str((price - TICK).normalize()),
                                "ask": str((price + TICK).normalize()),
                                "bid_qty": "1.000",
                                "ask_qty": "1.000",
                            },
                            sort_keys=True,
                        )
                        + "\n"
                    )
                    n_q += 1
                flush_before(ts_ms, fh)
                qty = Decimal(row["quantity"]).quantize(Decimal("0.001"))
                fh.write(
                    json.dumps(
                        {
                            "kind": "Trade",
                            "ts_ns": str(ts_ms * 1_000_000),
                            "price": str(price.normalize()),
                            "qty": str(qty),
                            "aggressor": "SELL" if row["is_buyer_maker"] == "true" else "BUY",
                            "trade_id": row["agg_trade_id"],
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
                n_t += 1
            while pending_idx < len(pending):
                fh.write(json.dumps(pending[pending_idx][2], sort_keys=True) + "\n")
                pending_idx += 1
    except Exception:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
    n_m = sum(1 for _, order, _ in pending if order == 0)
    n_f = sum(1 for _, order, _ in pending if order == 1)
    tmp.rename(out)
    return {"day": day, "status": "ok", "quotes": n_q, "trades": n_t, "marks": n_m, "funding": n_f, "funding_day": day, "funding_window_ms": [start_ms, end_ms], "mark_source": mark_source, "mark_label": mark_label, "bytes": out.stat().st_size, "sha256": sha.hexdigest()}


class _HashWriter:
    """Write-through wrapper that hashes every byte written."""

    def __init__(self, raw, sha):
        self._raw = raw
        self._sha = sha

    def write(self, s: str) -> None:
        self._sha.update(s.encode("utf-8"))
        self._raw.write(s)


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    missing = [d for d in NEEDED if (OUT / f"{d}.jsonl").is_file() is False or (OUT / f"{d}.jsonl").stat().st_size == 0]
    if args.limit:
        missing = missing[: args.limit]
    print(f"needed={len(NEEDED)} to_build={len(missing)}", flush=True)
    funding_rows = load_funding_rows(set(NEEDED))
    print(f"funding_rows={len(funding_rows)}", flush=True)
    results: dict[str, dict] = {}
    failures: list[str] = []
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(build_day, d, funding_for_day(funding_rows, d)): d for d in missing}
        for fut in as_completed(futures):
            day = futures[fut]
            try:
                entry = fut.result()
                results[day] = entry
                done += 1
                print(f"[{done}/{len(missing)}] {entry['status']} {day} q={entry.get('quotes')} t={entry.get('trades')} f={entry.get('funding')}", flush=True)
            except Exception as exc:
                failures.append(day)
                print(f"FAIL {day}: {exc!r}", flush=True)
    manifest = {
        "schema": "qel.m2_fold_tapes/1.0",
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "days": {d: results[d] for d in sorted(results)},
        "built_days": sum(1 for r in results.values() if r.get("status") in ("ok", "skip")),
        "failed_days": sorted(failures),
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print("DONE", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
