"""A1: PIT symbol feasibility report tests."""

from __future__ import annotations

import sys
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from m2_symbol_feasibility import assess, build_report


def test_btc_20_usdt_is_infeasible_matches_existing_m2_gate() -> None:
    row = assess("BTCUSDT", "20")
    # Existing M2 check: 20 < 58.94189 min notional. Must be reported, not rounded up.
    assert row["status"] == "INFEASIBLE"
    assert row["reason"] == "INFEASIBLE_VENUE_MIN_NOTIONAL"
    assert Fraction(row["min_notional"]) == Fraction(row["min_qty"]) * Fraction(row["reference_price"])
    assert Fraction("20") < Fraction(row["min_notional"])


def test_btc_1000_usdt_is_eligible_at_same_reference() -> None:
    row = assess("BTCUSDT", "1000")
    assert row["status"] == "ELIGIBLE"
    assert row["feasible"] is True


def test_unpinned_symbol_is_unknown_never_eligible() -> None:
    row = assess("ETHUSDT", "1000")
    assert row["status"] == "UNKNOWN"
    assert row["provenance"] == "NONE"


def test_unknown_symbol_is_unknown() -> None:
    assert assess("DOGEUSDT", "20")["status"] == "UNKNOWN"


def test_report_is_deterministic() -> None:
    a = build_report(["20", "1000"], ["BTCUSDT", "ETHUSDT"])
    b = build_report(["20", "1000"], ["BTCUSDT", "ETHUSDT"])
    assert a == b
    assert a["verdict"] == "HAS_ELIGIBLE"
    assert any(r["status"] == "INFEASIBLE" for r in a["rows"])


def test_report_at_20_only_has_no_feasible_symbol() -> None:
    report = build_report(["20"], ["BTCUSDT"])
    assert report["verdict"] == "NO_FEASIBLE_SYMBOL_AT_PROVEN_MINIMUM"
