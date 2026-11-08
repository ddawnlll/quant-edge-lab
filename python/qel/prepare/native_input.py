"""Convert a normalized Parquet dataset into a native replay JSONL input."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pyarrow.parquet as pq

from ..contract.canonical import format_decimal

PRICE_EXPONENT = 8
QTY_EXPONENT = 8


def to_native_input(parquet_path: str, out_path: str) -> dict:
    table = pq.read_table(parquet_path)
    rows = table.to_pylist()
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with out.open("w") as f:
        for row in rows:
            kind = row["event_type"]
            if kind == "Quote":
                rec = {
                    "kind": "Quote",
                    "ts_ns": str(row["ts_ns"]),
                    "bid": format_decimal(int(row["bid_i"]), PRICE_EXPONENT),
                    "ask": format_decimal(int(row["ask_i"]), PRICE_EXPONENT),
                    "bid_qty": format_decimal(int(row["bid_qty_i"]), QTY_EXPONENT),
                    "ask_qty": format_decimal(int(row["ask_qty_i"]), QTY_EXPONENT),
                }
            elif kind == "Trade":
                rec = {
                    "kind": "Trade",
                    "ts_ns": str(row["ts_ns"]),
                    "price": format_decimal(int(row["price_i"]), PRICE_EXPONENT),
                    "qty": format_decimal(int(row["qty_i"]), QTY_EXPONENT),
                    "aggressor": row["aggressor"],
                    "trade_id": row["trade_id"],
                }
            elif kind == "Mark":
                rec = {
                    "kind": "Mark",
                    "ts_ns": str(row["ts_ns"]),
                    "price": format_decimal(int(row["price_i"]), PRICE_EXPONENT),
                }
            elif kind == "Funding":
                # The normalized funding row stores the rate coefficient at 1e-8.
                rec = {
                    "kind": "Funding",
                    "ts_ns": str(row["ts_ns"]),
                    "rate": format_decimal(int(row["price_i"]), 8),
                }
            else:
                continue
            f.write(json.dumps(rec, sort_keys=True) + "\n")
            count += 1
    h = hashlib.sha256(out.read_bytes()).hexdigest()
    return {"path": str(out), "records": count, "sha256": h}
