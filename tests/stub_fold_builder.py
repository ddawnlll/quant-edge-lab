"""Hermetic stand-in for scripts/m2_build_fold_tapes_stream.py.

Driver tests exercise the *driver* (checkpoints, resume, run identity,
aggregation). Building a real 1.25M-event archive day for every driver test
adds minutes without testing anything the driver owns; tape construction has
its own byte-parity suite in tests/test_m2_fold_tapes.py.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

OUT = Path(".")
ARCH = Path(".")

def _day_start_ms(day: str) -> int:
    from datetime import date, datetime, time, timezone

    return int(datetime.combine(date.fromisoformat(day), time.min, timezone.utc).timestamp() * 1000)


def load_funding_rows(days):
    return [(_day_start_ms(day) + 3_600_000, "0.0001") for day in sorted(days)]


def funding_for_day(rows, day):
    start = _day_start_ms(day)
    return [(ts, rate) for ts, rate in rows if start <= ts < start + 86_400_000]


def build_day(day, funding_rows):
    out = Path(OUT) / f"{day}.jsonl"
    if out.is_file() and out.stat().st_size > 0:
        return {"day": day, "status": "skip"}
    lines = []
    day_start = _day_start_ms(day)
    for i in range(120):
        ts_ms = day_start + i * 60_000
        lines.append({"kind": "Quote", "ts_ns": str(ts_ms * 1_000_000), "bid": "100.00", "ask": "100.02",
                      "bid_qty": "1.000", "ask_qty": "1.000"})
        lines.append({"kind": "Trade", "ts_ns": str(ts_ms * 1_000_000 + 1), "price": "100.01",
                      "qty": "0.100", "aggressor": "BUY", "trade_id": str(i)})
    for ts_ms, rate in funding_rows:
        lines.append({"kind": "Funding", "ts_ns": str(ts_ms * 1_000_000), "rate": rate})
    payload = "".join(json.dumps(row, sort_keys=True) + "\n" for row in lines)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(payload)
    return {"day": day, "status": "ok", "quotes": 120, "trades": 120, "marks": 0,
            "funding": len(funding_rows), "bytes": len(payload),
            "sha256": hashlib.sha256(payload.encode()).hexdigest()}
