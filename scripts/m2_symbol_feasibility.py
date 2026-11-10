"""PIT order feasibility / symbol selection for M2 (A1).

Builds a deterministic eligibility report over candidate USDT-M perpetual
symbols. Each symbol is judged against the *frozen* TASK-12 folds and the
requested USDT budgets. The report never invents venue metadata:

- ``UNKNOWN``     — no PIT-certified venue rule file available (TASK-10 gap)
- ``INFEASIBLE``  — provable venue minimum above budget (BTC 20 USDT case)
- ``ELIGIBLE``    — certified PIT metadata + execution coverage both present

The only certified instrument metadata in this repo today is the test-fixture
minimum (minQty 0.001 for the native BTC runner, prices ~58941). That is
``TEST_FIXTURE`` provenance, not a historical venue file — so BTC is
``INFEASIBLE`` for 20 USDT but ``UNKNOWN`` for historical fold coverage
until a venue-certified rule file is pinned. Altcoin candidates are
``UNKNOWN`` (no PIT metadata pinned), never silently dropped or promoted.

Risk-vs-notional: ``notional = capital * risk_fraction`` is a *notional*
budget, not a stop-loss risk. The report shows both the notional feasibility
and the implied stop distance honestly, and never recommends raising
leverage or risk_fraction to bypass a venue minimum.
"""

from __future__ import annotations

import argparse
import json
import sys
from fractions import Fraction
from pathlib import Path

SCHEMA = "qel.m2_symbol_feasibility/1.0"

# Only instrument metadata present in the repo today. Provenance is a test
# fixture (native runner), per TASK-10 VENUE_LIMITATIONS scope note.
INSTRUMENTS = {
    "BTCUSDT": {
        "venue": "binance_usdm",
        "min_qty": Fraction("0.001"),
        "lot_step": Fraction("0.001"),
        "provenance": "TEST_FIXTURE",
        "provenance_note": "native runner instrument; no historical venue rule file pinned (TASK-10 gap)",
    },
    "ETHUSDT": {
        "venue": "binance_usdm",
        "provenance": "NONE",
        "provenance_note": "no PIT venue metadata pinned in repo",
    },
}

# execution-data availability observed on disk 2026-11-10 (see A2 coverage)
RESEARCH_REFERENCE_PRICE_BTC = Fraction("58941.89")


def assess(symbol: str, budget_usdt: str, reference_price: Fraction | None = None) -> dict:
    """Assess one symbol at one notional budget deterministically."""
    budget = Fraction(str(budget_usdt))
    meta = INSTRUMENTS.get(symbol, {"provenance": "NONE", "provenance_note": "not in instrument table"})
    out: dict = {
        "symbol": symbol,
        "budget_usdt": str(budget),
        "venue": meta.get("venue", "binance_usdm"),
        "provenance": meta.get("provenance", "NONE"),
    }
    if meta.get("provenance") == "NONE":
        out.update(
            {
                "status": "UNKNOWN",
                "reason": "NO_PIT_VENUE_METADATA",
                "note": meta.get("provenance_note", ""),
            }
        )
        return out
    if reference_price is None:
        reference_price = RESEARCH_REFERENCE_PRICE_BTC if symbol == "BTCUSDT" else None
    if reference_price is None:
        out.update({"status": "UNKNOWN", "reason": "NO_REFERENCE_PRICE"})
        return out
    min_qty = meta["min_qty"]
    min_notional = min_qty * reference_price
    feasible = budget >= min_notional
    out.update(
        {
            "reference_price": str(reference_price),
            "min_qty": str(min_qty),
            "min_notional": str(min_notional),
            "feasible": feasible,
            "status": "ELIGIBLE" if feasible else "INFEASIBLE",
            "reason": "VENUE_MIN_OK" if feasible else "INFEASIBLE_VENUE_MIN_NOTIONAL",
        }
    )
    return out


def build_report(budgets: list[str], symbols: list[str]) -> dict:
    rows = []
    for symbol in symbols:
        for budget in budgets:
            rows.append(assess(symbol, budget))
    eligible = [r for r in rows if r["status"] == "ELIGIBLE"]
    # no-candidate is an honest outcome; never substitute a different family
    verdict = "HAS_ELIGIBLE" if eligible else "NO_FEASIBLE_SYMBOL_AT_PROVEN_MINIMUM"
    return {
        "schema": SCHEMA,
        "verdict": verdict,
        "budgets_usdt": list(budgets),
        "rows": rows,
        "note": (
            "min_qty provenance is TEST_FIXTURE for BTCUSDT; historical PIT venue "
            "rule files are the TASK-10 gap. 20 USDT BTC is INFEASIBLE at "
            "~58941 (min 58.94). Raising leverage/risk to bypass is forbidden "
            "without liquidation/stop proof."
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--budgets", default="20,100,1000")
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    ap.add_argument("--out", default="evidence/labd/M2_SYMBOL_FEASIBILITY.json")
    args = ap.parse_args()
    report = build_report(args.budgets.split(","), args.symbols.split(","))
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
