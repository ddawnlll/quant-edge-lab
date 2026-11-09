"""TASK-13 multi-day control evidence tests.

These tests extend the single-day control-protocol suite to a multi-day UTC
calendar.  They pin:

* batch and sequential execution producing the same daily calendar series and
  the same frozen-seed series across several days,
* a zero-trade day staying in the series as zero,
* a missing mark staying null instead of being filled with zero,
* future price and candidate-fill mutation not changing control selection,
* per-day/per-stratum exposure and rejected-order diagnostics,
* financial-trial and cost-reservation preservation on a multi-day run,
* the bounded-denominator EMA required for multi-day tapes.
"""

from __future__ import annotations

import datetime
import json
import math

from qel.research import control
from qel.research.control import (
    _ema,
    matched_controls,
    prepare_control_plan,
    run_control_seed,
)

UTC = datetime.timezone.utc
START = datetime.datetime(2026, 10, 30, tzinfo=UTC)
DAY_NS = 86_400_000_000_000


# ---------------------------------------------------------------------------
# Tape helpers
# ---------------------------------------------------------------------------


def _write_tape(path, records):
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return str(path)


def _quote_rows(days=3, per_day=48, step_seconds=60, start=START):
    rows = []
    for day in range(days):
        for i in range(per_day):
            ts = start + datetime.timedelta(days=day, seconds=i * step_seconds)
            mid = 100.0 + day * 0.5 + 0.4 * math.sin((i + 1) / 4.0) + 0.01 * i
            bid = round(mid - 0.05, 2)
            ask = round(mid + 0.05, 2)
            rows.append(
                {
                    "kind": "Quote",
                    "ts_ns": str(int(ts.timestamp() * 1_000_000_000)),
                    "bid": f"{bid:.2f}",
                    "ask": f"{ask:.2f}",
                }
            )
    return rows


def _multi_day_tape(tmp_path, days=3, per_day=48, name="multi.jsonl"):
    return _write_tape(tmp_path / name, _quote_rows(days=days, per_day=per_day))


def _plan_rows(rows, after_ns=None, factor=1.0):
    out = []
    for row in rows:
        if row.get("kind") != "Quote":
            continue
        ts = int(row["ts_ns"])
        bid = float(row["bid"])
        ask = float(row["ask"])
        if after_ns is not None and ts > after_ns:
            # Keep exchange-tick precision so the account arithmetic stays a
            # terminating decimal, exactly like the real tapes.
            bid = round(bid * factor, 2)
            ask = round(ask * factor, 2)
        out.append({"ts": ts, "bid": bid, "ask": ask})
    return out


def _end_of_day(day_index, start=START):
    boundary = start + datetime.timedelta(days=day_index + 1)
    return int(boundary.timestamp() * 1_000_000_000) - 1


# ---------------------------------------------------------------------------
# Multi-day batch / sequential parity
# ---------------------------------------------------------------------------


def test_multiday_batch_and_sequential_match_across_calendar(tmp_path):
    events = _multi_day_tape(tmp_path, days=3, per_day=48)
    report = matched_controls(
        events,
        trades=6,
        notional=100,
        seed=13,
        replications=8,
        compare_executions=True,
    )
    series = report["daily_series"]
    assert [row["day"] for row in series] == ["2026-10-30", "2026-10-31", "2026-11-01"]
    assert len(series) == 3
    comparison = report["batch_vs_sequential"]
    assert comparison["series_equal"] is True
    assert comparison["seed_series_equal"] is True
    assert comparison["equal"] is True
    assert comparison["batch_series_hash"] == comparison["sequential_series_hash"]
    assert comparison["batch_completed"] == comparison["sequential_completed"] == 8
    assert report["diagnostics"]["retained_seed_series"] == 0


def test_multiday_zero_trade_training_day_stays_in_series(tmp_path):
    events = _multi_day_tape(tmp_path, days=3, per_day=48)
    report = matched_controls(
        events,
        trades=6,
        notional=100,
        seed=5,
        replications=4,
        train_end_ns=_end_of_day(0),
    )
    series = {row["day"]: row for row in report["daily_series"]}
    assert len(series) == 3
    assert series["2026-10-30"]["trades"] == 0
    assert series["2026-10-30"]["mean_net_pnl"] == "0"
    assert series["2026-10-30"]["zero_trade_seeds"] == 4
    for day in ("2026-10-31", "2026-11-01"):
        assert series[day]["trades"] > 0
        assert series[day]["target_trades"] == series[day]["trades"]


def test_multiday_missing_mark_is_not_filled_with_zero(tmp_path):
    rows = _quote_rows(days=3, per_day=48)
    # Make the whole second day an unusable mark: every exit quote is crossed.
    second_day_start = int((START + datetime.timedelta(days=1)).timestamp() * 1_000_000_000)
    second_day_end = int((START + datetime.timedelta(days=2)).timestamp() * 1_000_000_000)
    for row in rows:
        ts = int(row["ts_ns"])
        if second_day_start <= ts < second_day_end:
            row["bid"] = "101.00"
            row["ask"] = "100.00"
    events = _write_tape(tmp_path / "missing.jsonl", rows)
    report = matched_controls(
        events,
        trades=6,
        notional=100,
        seed=9,
        replications=4,
        train_end_ns=_end_of_day(0),
        compare_executions=True,
    )
    series = {row["day"]: row for row in report["daily_series"]}
    assert len(series) == 3
    assert series["2026-10-31"]["missing_seeds"] == 4
    assert series["2026-10-31"]["valid_seeds"] == 0
    assert series["2026-10-31"]["mean_net_pnl"] is None
    assert report["diagnostics"]["missing_mark_seeds"] == 4
    assert report["batch_vs_sequential"]["equal"] is True


# ---------------------------------------------------------------------------
# No-lookahead
# ---------------------------------------------------------------------------


def test_multiday_future_price_mutation_does_not_change_selection(tmp_path):
    rows = _quote_rows(days=3, per_day=48)
    base_input = _plan_rows(rows)
    plan = prepare_control_plan(base_input, 6, 100, 21)
    base = [run_control_seed(plan, i) for i in range(6)]
    mutated_input = _plan_rows(rows, after_ns=plan.train_end_ns, factor=1.7)
    mutated_plan = prepare_control_plan(mutated_input, 6, 100, 21)
    mutated = [run_control_seed(mutated_plan, i) for i in range(6)]
    assert mutated_plan.selection_plan_hash == plan.selection_plan_hash
    assert mutated_plan.plan_hash == plan.plan_hash
    assert [r.selection_hash for r in mutated] == [r.selection_hash for r in base]
    # Prices did change, so realised PnL is allowed to change; selection is not.
    assert mutated_plan.calendar == plan.calendar


def test_multiday_candidate_fills_never_select_controls(tmp_path):
    rows = _quote_rows(days=3, per_day=48)
    base_input = _plan_rows(rows)
    base = prepare_control_plan(base_input, 6, 100, 22)
    fake_fills = [
        {"ts": int(rows[-1]["ts_ns"]), "side": 1, "qty": 999},
        {"ts": int(rows[0]["ts_ns"]), "side": -1, "qty": 5},
    ]
    with_fills = prepare_control_plan(
        base_input, 6, 100, 22, candidate_fills=fake_fills
    )
    assert with_fills.selection_plan_hash == base.selection_plan_hash
    assert with_fills.plan_hash == base.plan_hash
    assert with_fills.candidate_fills_used is False
    assert [run_control_seed(with_fills, i).selection_hash for i in range(4)] == [
        run_control_seed(base, i).selection_hash for i in range(4)
    ]


# ---------------------------------------------------------------------------
# Diagnostics, resourcing and trials
# ---------------------------------------------------------------------------


def test_multiday_per_day_and_per_stratum_diagnostics(tmp_path):
    events = _multi_day_tape(tmp_path, days=3, per_day=48)
    report = matched_controls(events, trades=6, notional=100, seed=17, replications=6)
    traded = [
        row
        for row in report["daily_series"]
        if row["trades"] > 0
    ]
    assert len(traded) >= 2
    for row in traded:
        assert row["exposure_ns"] > 0
        assert row["holding_ns"] > 0
        assert row["exposure_notional_ns"] == 100 * row["exposure_ns"]
        assert row["target_trades"] == row["trades"]
    assert report["per_stratum"]
    assert sum(row["trades"] for row in report["per_stratum"]) == report["diagnostics"][
        "executed_trades"
    ]
    assert report["holding_time_achieved_ns"] > 0


def test_multiday_financial_trials_and_cost_reservations_preserved(tmp_path):
    events = _multi_day_tape(tmp_path, days=3, per_day=48)
    report = matched_controls(events, trades=6, notional=100, seed=19, replications=5)
    trials = report["financial_trials"]
    assert trials["preserved_count"] == trials["input_count"] == 5
    assert trials["dropped"] == 0
    assert trials["trial_hash"]
    reservations = report["cost_reservations"]
    assert reservations["reservation_hash"]
    target_total = sum(report["trade_count_target_per_day"].values())
    assert reservations["derived"]["target_trades"] == target_total
    assert reservations["derived"]["replications"] == 5
    assert report["status"] == "INCOMPLETE_EVIDENCE"
    assert "SHORT_SEED_RUN" in report["incomplete_reasons"]


def test_multiday_trend_pullback_control_is_separate(tmp_path):
    events = _multi_day_tape(tmp_path, days=3, per_day=48)
    report = matched_controls(events, trades=6, notional=100, seed=23, replications=4)
    trend = report["frozen_trend_pullback_control"]
    assert trend["separate_from_random_controls"] is True
    assert trend["included_in_random_control_mean"] is False
    assert [row["day"] for row in trend["daily_series"]] == [
        "2026-10-30",
        "2026-10-31",
        "2026-11-01",
    ]
    assert trend["daily_series_hash"]


# ---------------------------------------------------------------------------
# Multi-day EMA bound
# ---------------------------------------------------------------------------


def test_ema_denominator_is_bounded_on_long_multiday_series():
    from fractions import Fraction

    values = [Fraction(100_000 + i, 1000) for i in range(20_000)]
    out = _ema(values, 20)
    assert len(out) == len(values)
    # Naive exact Fraction EMA grows ~4.4 bits per step (~88k bits here) and
    # never finishes on a multi-day tape.  The frozen 1e-9 quantisation keeps
    # the denominator at exactly 1e9.
    assert all(1_000_000_000 % value.denominator == 0 for value in out)
    assert out[-1].denominator.bit_length() < 64


def test_day_of_is_utc_and_cached():
    ts = int(datetime.datetime(2026, 11, 2, 23, 59, 59, tzinfo=UTC).timestamp() * 1_000_000_000)
    assert control._day_of(ts) == "2026-11-02"
    assert control._day_of(ts + 1_000_000_000) == "2026-11-03"
    assert control._day_of(0) == "1970-01-01"
