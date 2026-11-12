"""P3 causal features: parity vs control Fraction EMA, no future leakage."""

from __future__ import annotations

from fractions import Fraction

import numpy as np
import pyarrow as pa

from qel.prepare.archive_parquet import SCHEMA
from qel.research.arrow_features import (
    EmaState,
    ema_over_batches,
    ema_streaming,
    rolling_mean_var,
    signal_above_below,
)


def _batch(kinds, bids, asks):
    n = len(kinds)
    cols = {
        "event_type": kinds,
        "ts_ns": list(range(n)),
        "recv_ns": list(range(n)),
        "source_ordinal": list(range(n)),
        "price_i": [0] * n,
        "qty_i": [0] * n,
        "bid_i": bids,
        "ask_i": asks,
        "bid_qty_i": [100_000_000] * n,
        "ask_qty_i": [100_000_000] * n,
        "aggressor": [""] * n,
        "trade_id": [""] * n,
        "source_id": ["BINANCE_USDM_PUBLIC_ARCHIVE"] * n,
        "symbol": ["BTCUSDT"] * n,
        "fidelity": ["assumed_synthetic_l1_around_trade"] * n,
    }
    return pa.RecordBatch.from_pydict(cols, schema=SCHEMA)


def test_ema_matches_fraction_reference_exactly():
    # Mids as exact Fractions (control path) vs integer streaming grid.
    mids_coeff = [100_00000000, 100_50000000, 101_00000000, 100_00000000, 99_00000000]
    mids = [Fraction(c, 10**8) for c in mids_coeff]
    ref = [Fraction(u, 10**9) for u in ema_streaming(mids_coeff)]
    from qel.research.arrow_features import ema_from_fraction_mids

    expected = ema_from_fraction_mids(mids)
    assert ref == expected


def test_ema_causal_no_future_leak():
    a = ema_streaming([100_00000000, 101_00000000, 102_00000000] + [200_00000000] * 5)
    b = ema_streaming([100_00000000, 101_00000000, 102_00000000] + [0] * 5)
    # First 3 outputs identical regardless of future.
    assert a[:3] == b[:3]
    assert a[3:] != b[3:]


def test_ema_batch_boundary_carry():
    quotes = [(100_00000000 + i, 100_01000000 + i) for i in range(7)]
    one = [b for b in quotes]
    whole = _batch(["Quote"] * 7, [q[0] for q in quotes], [q[1] for q in quotes])
    split_a = _batch(["Quote"] * 3, [q[0] for q in quotes[:3]], [q[1] for q in quotes[:3]])
    split_b = _batch(["Quote"] * 4, [q[0] for q in quotes[3:]], [q[1] for q in quotes[3:]])
    assert ema_over_batches([whole]) == ema_over_batches([split_a, split_b])


def test_rolling_no_future_leak():
    v = np.array([1.0, 2.0, 100.0, 4.0])
    mean, var = rolling_mean_var(v, 3)
    # First two means use only past.
    assert mean[0] == 1.0
    assert mean[1] == 1.5
    # Changing future (index 3) does not change mean[2].
    mean2, _ = rolling_mean_var(np.array([1.0, 2.0, 100.0, -999.0]), 3)
    assert mean[2] == mean2[2]


def test_signal_labels():
    mids = [100_00000000, 101_00000000]
    ema = ema_streaming(mids)
    assert signal_above_below(mids, ema)[0] == "EQUAL"  # first bar seeds state
    assert signal_above_below(mids, ema)[1] == "ABOVE"


def test_mids_extraction_filters_non_quotes_and_odd_sums():
    from qel.research.arrow_features import mids_from_batch, mids_units_1e9_from_batch

    batch = _batch(
        ["Quote", "Trade", "Quote", "Mark"],
        [100, 999, 101, 888],
        [201, 999, 200, 888],
    )
    # Quote rows only: (100+201)//2=150 (floor), (101+200)//2=150.
    assert mids_from_batch(batch) == [150, 150]
    # Exact 1e-9 units: (100+201)*5=1505, (101+200)*5=1505.
    assert mids_units_1e9_from_batch(batch) == [1505, 1505]


def test_rolling_matches_naive_loop():
    rng_data = np.array([3.0, 1.0, 4.0, 1.0, 5.0, 9.0, 2.0, 6.0])
    mean, var = rolling_mean_var(rng_data, 4)
    for i in range(len(rng_data)):
        lo = max(0, i - 4 + 1)
        seg = rng_data[lo : i + 1]
        assert mean[i] == seg.mean()
        assert np.isclose(var[i], seg.var(), rtol=1e-12, atol=1e-12)
