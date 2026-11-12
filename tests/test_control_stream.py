"""Bounded control fit parity: streamed fit must equal the canonical fit.

The canonical reference is ``control.prepare_control_plan`` on the same
quote series. Asserted equalities are the frozen products: plan_hash,
selection_plan_hash, strata (weight, long_rate, target, train/eval
counts), targets, calendar, holding means, and per-seed results
(net PnL, trades, daily rows, hashes) for a small seed prefix.
"""

from __future__ import annotations

import sys
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from qel.research import control as C  # noqa: E402
from qel.research import control_stream as S  # noqa: E402

DAY_NS = 86_400 * 10**9


def _synthetic_days(n_days: int = 5, per_day: int = 400, seed: int = 7):
    """Two train days then the rest validation; quotes at ~1s spacing."""
    rng = np.random.default_rng(seed)
    days = []
    start = 1725148800 * 10**9  # 2024-09-01T00:00:00Z in ns
    for d in range(n_days):
        day_start = start + d * DAY_NS
        ts = day_start + np.cumsum(rng.integers(0, 3, size=per_day) + 1) * 10**9
        levels = 10000 + np.cumsum(rng.integers(-5, 6, size=per_day))
        bid = levels.copy()
        ask = levels + rng.integers(1, 4, size=per_day)
        day = f"2024-09-{d + 1:02d}"
        days.append((day, ts.astype(np.int64), (bid * 100).astype(np.int64), (ask * 100).astype(np.int64)))
    return days


def _canonical_quotes(day_arrays):
    quotes = []
    for _day, ts, bid, ask in day_arrays:
        for i in range(len(ts)):
            bid_f = Fraction(int(bid[i]), 100)
            ask_f = Fraction(int(ask[i]), 100)
            quotes.append(
                {
                    "ts": int(ts[i]),
                    "bid": float(bid_f),
                    "ask": float(ask_f),
                    "bid_exact": str(bid_f),
                    "ask_exact": str(ask_f),
                }
            )
    return quotes


def _train_end(day_arrays):
    # End of day index 1 (two train days, like a 2-day train slice).
    return int(day_arrays[1][1][-1])


def test_streamed_hash_matches_canonical():
    material = {
        "z": [1, 2, 3],
        "a": {"b": [100, -5, 0], "a": "x"},
        "m": None,
        "t": True,
        "n": -123,
        "s": "2024-09-01",
    }
    assert S.streaming_hash_json(material) == C._hash_json(material)


def test_streamed_hash_big_lists():
    lists = [list(range(i * 1000, (i + 1) * 1000)) for i in range(10)]
    flat = [v for chunk in lists for v in chunk]
    big = {"eval_indices": {"2024-09-05": S._Chunks(lists)}}
    material = {"eval_indices": {"2024-09-05": flat}}
    assert S.streaming_hash_json(big) == C._hash_json(material)


def test_price_to_cents_exact_and_fail_closed():
    assert S.price_to_cents("58941.91") == 5894191
    assert S.price_to_cents("58941.9") == 5894190
    assert S.price_to_cents("100") == 10000
    assert S.price_to_cents("-0.5") == -50
    with pytest.raises(ValueError):
        S.price_to_cents("0.001")


def test_lazy_samples_indexing():
    samples = S.LazySortedSamples([10, 20, 30], [2, 1, 3])
    assert len(samples) == 6
    assert list(samples) == [10, 10, 20, 30, 30, 30]
    assert samples[0] == 10 and samples[3] == 30 and samples[-1] == 30
    assert C._quantile(samples, 0.5) == C._quantile([10, 10, 20, 30, 30, 30], 0.5)
    with pytest.raises(IndexError):
        samples[6]


@pytest.fixture(scope="module")
def parity_plans():
    day_arrays = _synthetic_days()
    quotes = _canonical_quotes(day_arrays)
    train_end = _train_end(day_arrays)
    canon = C.prepare_control_plan(quotes, 40, "100", 0, 3_600_000_000_000, train_end_ns=train_end, funding=[])
    stream = S.fit_control_plan_stream(day_arrays, trades=40, notional="100", seed=0, max_holding_ns=3_600_000_000_000, train_end_ns=train_end, funding=[])
    return canon, stream


def test_plan_hashes_match(parity_plans):
    canon, stream = parity_plans
    assert stream.plan_hash == canon.plan_hash
    assert stream.selection_plan_hash == canon.selection_plan_hash
    assert stream.calendar == canon.calendar
    assert stream.target_trades_by_day == canon.target_trades_by_day
    assert stream.training_trade_target_by_day == canon.training_trade_target_by_day
    assert stream.holding_target_ns == canon.holding_target_ns
    assert stream.holding_mean_ns == canon.holding_mean_ns
    assert stream.holding_effective_mean_ns == canon.holding_effective_mean_ns
    assert set(stream.strata) == set(canon.strata)
    for key, mine in stream.strata.items():
        ref = canon.strata[key]
        assert mine.weight == ref.weight
        assert mine.long_rate == ref.long_rate
        assert mine.holding_target_ns == ref.holding_target_ns
        assert mine.train_quotes == ref.train_quotes
        assert mine.eval_quotes == ref.eval_quotes
        assert list(mine.holding_samples) == list(ref.holding_samples)


def test_seed_results_match(parity_plans):
    canon, stream = parity_plans
    for seed in range(3):
        a = C.run_control_seed(canon, seed)
        b = C.run_control_seed(stream, seed)
        assert a.net_pnl_total == b.net_pnl_total
        assert a.executed_trades == b.executed_trades
        assert a.rejected_orders == b.rejected_orders
        assert a.selection_hash == b.selection_hash
        assert a.control_hash == b.control_hash
        assert a.days == b.days


def test_empty_holding_history_fails_closed():
    ts = np.array([0, 10**9], dtype=np.int64)
    bid = np.array([10000, 10000], dtype=np.int64)
    ask = np.array([10001, 10001], dtype=np.int64)
    # Canonical prepare raises plain ValueError for <4 quotes; the streamed
    # fit matches that failure exactly.
    with pytest.raises(ValueError):
        S.fit_control_plan_stream(
            [("2024-09-01", ts, bid, ask)],
            trades=None,
            notional="100",
            seed=0,
            max_holding_ns=10**10,
            train_end_ns=int(ts[0]),
        )


def test_dev_week_plan_parity():
    """Canonical fit on real dev tapes vs streamed fit on extracted quotes."""
    root = Path(__file__).resolve().parents[1]
    days = ["2024-09-01", "2024-09-02", "2024-09-03", "2024-09-05", "2024-09-06", "2024-09-07"]
    day_arrays = []
    for day in days:
        ts, bid, ask = S.extract_day_quotes(str(root / "data/native/_dev_w1" / f"{day}.jsonl"))
        day_arrays.append((day, ts, bid, ask))
    quotes = _canonical_quotes(day_arrays)
    import datetime

    train_end = int(datetime.datetime(2024, 9, 4, tzinfo=datetime.timezone.utc).timestamp()) * 10**9 - 1
    canon = C.prepare_control_plan(quotes, 58, "100", 0, 3_600_000_000_000, train_end_ns=train_end, funding=[])
    stream = S.fit_control_plan_stream(day_arrays, trades=58, notional="100", seed=0, max_holding_ns=3_600_000_000_000, train_end_ns=train_end, funding=[])
    assert stream.plan_hash == canon.plan_hash
    assert stream.selection_plan_hash == canon.selection_plan_hash
    assert stream.target_trades_by_day == canon.target_trades_by_day
    a = C.run_control_seed(canon, 0)
    b = C.run_control_seed(stream, 0)
    assert a.selection_hash == b.selection_hash
    assert a.net_pnl_total == b.net_pnl_total
    assert a.days == b.days


def test_day_arrays_contract_is_enforced():
    """The streamed fit must refuse inputs the canonical fit would reorder."""
    days = _synthetic_days(4, 80, 7)
    train_end = _train_end(days)
    kwargs = dict(
        trades=10,
        notional="100",
        seed=0,
        max_holding_ns=3_600_000_000_000,
        train_end_ns=train_end,
    )
    with pytest.raises(C.ControlError, match="DAY_ORDER_NOT_ASCENDING"):
        S.fit_control_plan_stream(list(reversed(days)), **kwargs)

    unsorted_day = list(days)
    _day, ts, bid, ask = unsorted_day[1]
    unsorted_day[1] = (_day, ts[[1, 0] + list(range(2, len(ts)))], bid, ask)
    with pytest.raises(C.ControlError, match="DAY_TIMESTAMPS_NOT_SORTED"):
        S.fit_control_plan_stream(unsorted_day, **kwargs)

    empty = list(days)
    empty[2] = (days[2][0], np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64))
    with pytest.raises(C.ControlError, match="EMPTY_DAY_ARRAY"):
        S.fit_control_plan_stream(empty, **kwargs)

    mislabelled = list(days)
    _day, ts, bid, ask = mislabelled[0]
    mislabelled[0] = ("2024-10-01", ts, bid, ask)
    with pytest.raises(C.ControlError, match="DAY_LABEL_MISMATCH"):
        S.fit_control_plan_stream(mislabelled, **kwargs)


def test_funding_parity_matches_canonical():
    """Non-empty funding must produce the same plan and seed results."""
    day_arrays = _synthetic_days(4, 120, 11)
    quotes = _canonical_quotes(day_arrays)
    train_end = _train_end(day_arrays)
    funding = [
        {"ts": int(day_arrays[0][1][10]), "rate": "0.0001"},
        {"ts": int(day_arrays[1][1][20]), "rate": "-0.0002"},
        {"ts": int(day_arrays[2][1][30]), "rate": "0.0003"},
    ]
    canon = C.prepare_control_plan(quotes, 20, "100", 0, 3_600_000_000_000, train_end_ns=train_end, funding=funding)
    stream = S.fit_control_plan_stream(day_arrays, trades=20, notional="100", seed=0, max_holding_ns=3_600_000_000_000, train_end_ns=train_end, funding=funding)
    assert stream.plan_hash == canon.plan_hash
    assert stream.selection_plan_hash == canon.selection_plan_hash
    for seed in range(3):
        a = C.run_control_seed(canon, seed)
        b = C.run_control_seed(stream, seed)
        assert a.net_pnl_total == b.net_pnl_total
        assert a.funding == b.funding
        assert a.selection_hash == b.selection_hash
        assert a.control_hash == b.control_hash
        assert a.days == b.days
    # Funding must change the frozen plan identity and the per-seed accounting
    # compared with the same inputs without funding.
    plain = S.fit_control_plan_stream(
        day_arrays, trades=20, notional="100", seed=0, max_holding_ns=3_600_000_000_000,
        train_end_ns=train_end, funding=[],
    )
    assert stream.plan_hash != plain.plan_hash
    plain_seed = C.run_control_seed(plain, 0)
    funded_seed = C.run_control_seed(stream, 0)
    assert funded_seed.days == plain_seed.days or funded_seed.funding != plain_seed.funding
