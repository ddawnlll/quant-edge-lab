"""Build a development native replay input from public Binance daily archives.

The archive has observed trades and mark prices, but no historical best bid/ask.
The L1 quote is therefore an explicit synthetic assumption around each last
trade; it must never be described as observed depth.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from decimal import Decimal
from pathlib import Path

TICK = Decimal("0.01")
QTY_EXPONENT = 3


def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _rows(zip_path: str):
    z = zipfile.ZipFile(zip_path)
    name = z.namelist()[0]
    with z.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"))


def build(agg_zip: str, mark_zip: str, funding_json: str, out_path: str, symbol: str = "BTCUSDT") -> dict:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    trades = list(_rows(agg_zip))
    marks = list(_rows(mark_zip))
    funding = json.loads(Path(funding_json).read_text())

    events = []
    # One synthetic top-of-book quote per second from the last observed trade.
    last_second = None
    for row in trades:
        ts = int(row["transact_time"])
        price = Decimal(row["price"])
        qty = Decimal(row["quantity"]).quantize(Decimal("0.001"))
        bucket = ts // 1000
        if last_second is None or bucket != last_second:
            last_second = bucket
            events.append(
                {
                    "kind": "Quote",
                    "ts_ns": str(last_second * 1_000_000_000),
                    "bid": str((price - TICK).normalize()),
                    "ask": str((price + TICK).normalize()),
                    "bid_qty": "1.000",
                    "ask_qty": "1.000",
                }
            )
        events.append(
            {
                "kind": "Trade",
                "ts_ns": str(ts * 1_000_000),
                "price": str(price.normalize()),
                "qty": str(qty),
                "aggressor": "SELL" if row["is_buyer_maker"] == "true" else "BUY",
                "trade_id": row["agg_trade_id"],
            }
        )
    for row in marks:
        events.append(
            {
                "kind": "Mark",
                "ts_ns": str(int(row["close_time"]) * 1_000_000),
                "price": str(Decimal(row["close"]).normalize()),
            }
        )
    for row in funding:
        events.append(
            {
                "kind": "Funding",
                "ts_ns": str(int(row["fundingTime"]) * 1_000_000),
                "rate": str(Decimal(row["fundingRate"]).normalize()),
            }
        )
    events.sort(key=lambda e: int(e["ts_ns"]))
    with out.open("w") as f:
        for event in events:
            f.write(json.dumps(event, sort_keys=True) + "\n")
    counts = {}
    for event in events:
        counts[event["kind"]] = counts.get(event["kind"], 0) + 1
    manifest = {
        "schema_version": "1.1.0",
        "dataset_id": "DS-ARCHIVE-BTCUSDT-2026-11-07",
        "symbol": symbol,
        "source": "BINANCE_USDM_PUBLIC_ARCHIVE",
        "input_hashes": {"agg_trades": _sha(agg_zip), "mark_klines": _sha(mark_zip), "funding": _sha(funding_json)},
        "native_input": str(out),
        "native_sha256": _sha(str(out)),
        "counts": counts,
        "time_range": {"start_ns": events[0]["ts_ns"], "end_ns": events[-1]["ts_ns"]},
        "fidelity": {
            "PRICE_PATH": "observed_trade",
            "BOOK_COVERAGE": "assumed_synthetic_l1_around_trade",
            "FEED_LATENCY": "assumed_zero",
            "FEES": "assumed_taker_0.0004",
            "FUNDING": "observed_venue_funding",
            "MARK": "observed_mark_price_kline_close",
            "IMPACT": "not_certified",
        },
        "claim_boundary": "synthetic L1 quote assumption; no observed depth or queue claim",
    }
    mpath = out.parent / "archive_native_manifest.json"
    mpath.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return {"manifest": str(mpath), "native": str(out), "counts": counts, "sha256": manifest["native_sha256"]}
