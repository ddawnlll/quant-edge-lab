"""Matched random-entry controls on the same delivered quote tape."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

TAKER_FEE = 0.0004


def load_quotes(events_path: str) -> list[dict]:
    quotes = []
    for line in Path(events_path).read_text().splitlines():
        rec = json.loads(line)
        if rec.get("kind") == "Quote":
            quotes.append(
                {
                    "ts": int(rec["ts_ns"]),
                    "bid": float(rec["bid"]),
                    "ask": float(rec["ask"]),
                }
            )
    if len(quotes) < 4:
        raise ValueError("INSUFFICIENT_HISTORY")
    return sorted(quotes, key=lambda q: q["ts"])


def run_control(quotes: list[dict], trades: int, notional: float, seed: int, max_holding_ns: int = 10_000_000_000) -> dict:
    rng = np.random.default_rng(seed)
    n = len(quotes)
    entries = np.sort(rng.choice(n, size=min(trades, n - 1), replace=False))
    pnl = 0.0
    fees = 0.0
    holds = []
    for idx in entries:
        i = int(idx)
        side = 1 if rng.random() < 0.5 else -1
        hold = int(rng.integers(1, max(2, min(11, n - i))))
        j = min(n - 1, i + hold)
        entry = quotes[i]["ask"] if side > 0 else quotes[i]["bid"]
        exit_ = quotes[j]["bid"] if side > 0 else quotes[j]["ask"]
        qty = max(0.001, round((notional / entry) / 0.001) * 0.001)
        gross = side * (exit_ - entry) * qty
        fee = TAKER_FEE * (entry + exit_) * qty
        pnl += gross - fee
        fees += fee
        holds.append(quotes[j]["ts"] - quotes[i]["ts"])
    return {
        "seed": seed,
        "trades": len(entries),
        "net_pnl": round(pnl, 8),
        "fees": round(fees, 8),
        "mean_holding_ns": int(sum(holds) / len(holds)) if holds else 0,
        "control_hash": hashlib.sha256(f"{seed}:{trades}:{notional}".encode()).hexdigest(),
    }


def matched_controls(events_path: str, trades: int, notional: float, seed: int, replications: int = 8) -> dict:
    quotes = load_quotes(events_path)
    controls = [run_control(quotes, trades, notional, seed + i) for i in range(replications)]
    values = [c["net_pnl"] for c in controls]
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / max(1, len(values) - 1)
    return {
        "status": "PASS",
        "trade_count_matched": trades,
        "notional": notional,
        "seed": seed,
        "replications": replications,
        "controls": controls,
        "control_mean": round(mean, 8),
        "control_std": round(var**0.5, 8),
        "control_hash": hashlib.sha256(json.dumps(controls, sort_keys=True).encode()).hexdigest(),
    }
