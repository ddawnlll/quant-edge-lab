"""Funding-carry screening skeleton over 1m mark prices.

This is a *screening* tool, not a backtest of record and not a claim. It
evaluates one hypothesis with explicit costs:

    Hold against the funding sign around each settlement: short when the
    funding rate is positive (longs pay shorts), long when it is negative.
    The position is opened at the mark close of the settlement minute and
    closed at the mark close of the next settlement minute in the same day.

Costs are charged explicitly per side:

* taker fee ``TAKER_FEE_RATE`` of notional,
* half-spread ``SPREAD_TICKS * TICK / price`` of notional (one tick per side).

No pandas/polars is installed in this environment and adding one would be a new
dependency, so the arithmetic uses numpy (already a project dependency). The
loop is over settlements (3/day), while prices are loaded as arrays; this is a
screening skeleton, not a low-latency path.
"""

from __future__ import annotations

import csv
import io
import json
import sys
import zipfile
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import numpy as np

TICK = 0.01
TAKER_FEE_RATE = 0.0004
SPREAD_TICKS = 1.0
DEFAULT_NOTIONAL = 1000.0
SETTLEMENT_MS = 8 * 3600 * 1000


@dataclass(frozen=True)
class Costs:
    fee_rate: float = TAKER_FEE_RATE
    spread_ticks: float = SPREAD_TICKS
    tick: float = TICK

    def per_side_fraction(self, price: float) -> float:
        """Fee + half-spread as a fraction of notional, for one side."""
        if price <= 0:
            raise ValueError("price must be positive")
        return self.fee_rate + (self.spread_ticks * self.tick / price) / 2.0

    def round_trip(self, notional: float, entry: float, exit: float) -> float:
        return notional * (self.per_side_fraction(entry) + self.per_side_fraction(exit))


def load_mark_minutes(mark_zip: str | Path) -> tuple[np.ndarray, np.ndarray]:
    """Return (close_time_ms, close_price) arrays from a markPriceKlines zip."""
    with zipfile.ZipFile(mark_zip) as z:
        with z.open(z.namelist()[0]) as fh:
            rows = list(csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8")))
    if not rows:
        raise ValueError(f"MARK_SOURCE_MISSING: {mark_zip} has no rows")
    for row in rows:
        if Decimal(row.get("volume", "0")) != 0 or Decimal(row.get("quote_volume", "0")) != 0:
            raise ValueError(f"MARK_SOURCE_MISSING: {mark_zip} looks like trade klines")
    ts = np.asarray([int(r["close_time"]) for r in rows], dtype=np.int64)
    px = np.asarray([float(r["close"]) for r in rows], dtype=np.float64)
    order = np.argsort(ts)
    return ts[order], px[order]


def load_funding(month_zip: str | Path) -> tuple[np.ndarray, np.ndarray]:
    """Return (calc_time_ms, funding_rate) arrays from a monthly fundingRate zip."""
    with zipfile.ZipFile(month_zip) as z:
        with z.open(z.namelist()[0]) as fh:
            rows = list(csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8")))
    ts = np.asarray([int(r["calc_time"]) for r in rows], dtype=np.int64)
    rate = np.asarray([float(r["last_funding_rate"]) for r in rows], dtype=np.float64)
    order = np.argsort(ts)
    return ts[order], rate[order]


def _price_at(ts: np.ndarray, px: np.ndarray, when_ms: int) -> float | None:
    """Latest mark close at or before ``when_ms`` (settlements sit between bars)."""
    i = int(np.searchsorted(ts, when_ms, side="right")) - 1
    if i < 0:
        # The 00:00 settlement precedes the first bar close (00:00:59.999).
        if len(ts) == 0:
            return None
        i = 0
    return float(px[i])


def run_day(
    day: str,
    mark_ts: np.ndarray,
    mark_px: np.ndarray,
    funding_ts: np.ndarray,
    funding_rate: np.ndarray,
    *,
    notional: float = DEFAULT_NOTIONAL,
    costs: Costs = Costs(),
) -> dict:
    """Evaluate the carry hypothesis for one UTC day. Vectorized over settlements."""
    start_ms = int(np.datetime64(day, "D").astype("datetime64[ms]").astype(np.int64))
    end_ms = start_ms + 86_400_000
    f_ts = funding_ts[(funding_ts >= start_ms) & (funding_ts < end_ms)]
    f_rate = funding_rate[(funding_ts >= start_ms) & (funding_ts < end_ms)]

    gross = 0.0
    funding_pnl = 0.0
    fees = 0.0
    trades = 0
    for i, when in enumerate(f_ts):
        entry = _price_at(mark_ts, mark_px, int(when))
        # Exit at the next settlement in the day, else the last mark minute.
        if i + 1 < len(f_ts):
            exit_at = int(f_ts[i + 1])
        else:
            exit_at = int(mark_ts[-1]) if len(mark_ts) else int(when)
        exit_px = _price_at(mark_ts, mark_px, exit_at)
        if entry is None or exit_px is None:
            continue
        direction = -1.0 if f_rate[i] > 0 else 1.0  # against the funding sign
        gross += direction * (exit_px - entry) / entry * notional
        funding_pnl += -direction * float(f_rate[i]) * notional
        fees += costs.round_trip(notional, entry, exit_px)
        trades += 1

    net = gross + funding_pnl - fees
    return {
        "day": day,
        "settlements": int(len(f_ts)),
        "trades": trades,
        "gross_pnl": round(gross, 8),
        "funding_pnl": round(funding_pnl, 8),
        "fees": round(fees, 8),
        "net_pnl": round(net, 8),
        "notional": notional,
    }


def screen_week(days: list[str], mark_root: str | Path, funding_zip: str | Path, *, out: str | Path | None = None) -> dict:
    mark_root = Path(mark_root)
    funding_ts, funding_rate = load_funding(funding_zip)
    results = []
    for day in days:
        mark_zip = mark_root / day / f"BTCUSDT-markPriceKlines-1m-{day}.zip"
        mark_ts, mark_px = load_mark_minutes(mark_zip)
        results.append(run_day(day, mark_ts, mark_px, funding_ts, funding_rate))
    payload = {
        "schema": "qel.screen_fast.funding_carry/1.0",
        "disclaimer": "screening only, no claims; development week; explicit costs",
        "costs": {
            "taker_fee_rate": TAKER_FEE_RATE,
            "spread_ticks_per_side": SPREAD_TICKS,
            "tick": TICK,
        },
        "days": results,
        "totals": {
            "gross_pnl": round(sum(r["gross_pnl"] for r in results), 8),
            "funding_pnl": round(sum(r["funding_pnl"] for r in results), 8),
            "fees": round(sum(r["fees"] for r in results), 8),
            "net_pnl": round(sum(r["net_pnl"] for r in results), 8),
        },
    }
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return payload


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: python -m qel.screen_fast.funding_carry <mark_root> <funding_zip> [--days d1 ...] [--out path]")
        return 2
    mark_root, funding_zip = argv[0], argv[1]
    days = [f"2024-09-0{i}" for i in range(1, 8)]
    out = None
    if "--days" in argv:
        i = argv.index("--days") + 1
        days = []
        while i < len(argv) and not argv[i].startswith("--"):
            days.append(argv[i])
            i += 1
    if "--out" in argv:
        out = argv[argv.index("--out") + 1]
    payload = screen_week(days, mark_root, funding_zip, out=out)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
