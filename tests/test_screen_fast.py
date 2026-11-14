"""Tests for the vectorized funding-carry screening skeleton (arithmetic only)."""

from __future__ import annotations

import numpy as np
import pytest

from qel.screen_fast.funding_carry import Costs, run_day

DAY_MS = 1725148800000
H8 = 8 * 3600 * 1000


def test_per_side_and_round_trip_costs() -> None:
    costs = Costs()
    # 4 bps taker + half of one tick (0.01 / 100 / 2 = 0.5 bp) = 4.5 bps.
    assert costs.per_side_fraction(100.0) == pytest.approx(0.00045)
    # Two sides on 1000 notional at a flat 100 price = 0.90 USDT.
    assert costs.round_trip(1000.0, 100.0, 100.0) == pytest.approx(0.90)


def test_positive_funding_shorts_and_receives_funding() -> None:
    mark_ts = np.array([DAY_MS, DAY_MS + H8], dtype=np.int64)
    mark_px = np.array([100.0, 100.0])
    funding_ts = np.array([DAY_MS, DAY_MS + H8], dtype=np.int64)
    funding_rate = np.array([0.001, 0.001])
    out = run_day("2024-09-01", mark_ts, mark_px, funding_ts, funding_rate)
    assert out["trades"] == 2
    assert out["gross_pnl"] == pytest.approx(0.0)
    assert out["funding_pnl"] == pytest.approx(2.0)  # 2 x 0.001 x 1000, short
    assert out["fees"] == pytest.approx(1.8)  # 2 round trips x 0.90
    assert out["net_pnl"] == pytest.approx(out["gross_pnl"] + out["funding_pnl"] - out["fees"])


def test_negative_funding_goes_long_and_price_move_is_signed() -> None:
    mark_ts = np.array([DAY_MS, DAY_MS + H8], dtype=np.int64)
    mark_px = np.array([100.0, 101.0])  # long gains 1%
    funding_ts = np.array([DAY_MS], dtype=np.int64)
    funding_rate = np.array([-0.001])
    out = run_day("2024-09-01", mark_ts, mark_px, funding_ts, funding_rate)
    assert out["trades"] == 1
    assert out["gross_pnl"] == pytest.approx(10.0)  # +1% x 1000, long
    assert out["funding_pnl"] == pytest.approx(1.0)  # long receives when rate < 0
    assert out["fees"] > 0
    assert out["net_pnl"] == pytest.approx(out["gross_pnl"] + out["funding_pnl"] - out["fees"])
