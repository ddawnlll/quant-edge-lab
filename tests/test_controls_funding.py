"""TASK-13 development-window funding and matched-control tests.

These tests pin the exact funding cost model added to the matched controls:

* long pays a positive rate and a short receives it,
* the amount is ``-side * entry_notional * rate`` in exact ``Fraction``
  arithmetic,
* settlements apply on the half-open window ``[entry_ts, exit_ts)``,
* duplicate tape funding events collapse by ``(ts_ns, rate)``,
* no settlement after exit (or after the tape end) can change a run,
* zero-trade days and missing marks keep their existing semantics,
* a bounded batch/sequential subset is identical with funding, and
* financial trials, cost reservations and per-day/per-stratum diagnostics are
  preserved.
"""

from __future__ import annotations

import datetime
import math
from fractions import Fraction

import pytest

from qel.research.control import (
    FUNDING_SIGN_CONVENTION,
    FUNDING_WINDOW,
    ControlAccount,
    funding_cash_delta,
    funding_window_indices,
    load_funding,
    matched_controls,
    normalize_funding,
    prepare_control_plan,
    run_control_seed,
)

UTC = datetime.timezone.utc
START = datetime.datetime(2026, 2, 1, tzinfo=UTC)
DAY_NS = 86_400_000_000_000


# ---------------------------------------------------------------------------
# Tape helpers
# ---------------------------------------------------------------------------


def _write_tape(path, records):
    path.write_text("\n".join(json_dumps(r) for r in records) + "\n")
    return str(path)


def json_dumps(record):
    import json

    return json.dumps(record)


def _quote_rows(days=3, per_day=48, step_seconds=60, start=START):
    rows = []
    for day in range(days):
        for i in range(per_day):
            ts = start + datetime.timedelta(days=day, seconds=i * step_seconds)
            mid = 100.0 + day * 0.6 + 0.4 * math.sin((i + 1) / 4.0) + 0.02 * i
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


def _funding_row(ts_ns, rate):
    return {"kind": "Funding", "ts_ns": str(int(ts_ns)), "rate": str(rate)}


def _midpoint_funding(quote_rows, offset_ns=30_000_000_000, rate="0.0001"):
    return [
        _funding_row(int(row["ts_ns"]) + offset_ns, rate)
        for row in quote_rows
        if row.get("kind") == "Quote"
    ]


def _tape(tmp_path, name="events.jsonl", funding=None):
    rows = _quote_rows(days=3, per_day=48)
    if funding is None:
        funding = _midpoint_funding(rows)
    return _write_tape(tmp_path / name, rows + list(funding))


def _quote_fingerprint(report):
    return [
        (
            row["day"],
            row["trades"],
            row["rejected_orders"],
            row["exposure_ns"],
            row["holding_ns"],
        )
        for row in report["daily_series"]
    ]


# ---------------------------------------------------------------------------
# Sign convention and exact amount
# ---------------------------------------------------------------------------


def test_funding_sign_convention_long_pays_short_receives():
    assert FUNDING_SIGN_CONVENTION == "LONG_PAYS_POSITIVE_RATE_SHORT_RECEIVES"
    assert FUNDING_WINDOW == "[entry_ts, exit_ts)"

    notional = Fraction(100)
    rate = Fraction(1, 1000)
    assert funding_cash_delta(1, notional, rate) == Fraction(-1, 10)
    assert funding_cash_delta(-1, notional, rate) == Fraction(1, 10)
    # A negative rate reverses the direction but not the convention.
    assert funding_cash_delta(1, notional, -rate) == Fraction(1, 10)
    assert funding_cash_delta(-1, notional, -rate) == Fraction(-1, 10)

    long_account = ControlAccount(currency="USDT", cash=Fraction(1000), initial_cash=Fraction(1000))
    assert long_account.open(1, Fraction(1), notional)
    assert long_account.apply_funding(rate, notional) == Fraction(-1, 10)
    assert long_account.funding == Fraction(-1, 10)
    assert long_account.cash == Fraction(1000) - Fraction(1, 10)
    long_account.close(Fraction(1), notional)

    short_account = ControlAccount(currency="USDT", cash=Fraction(1000), initial_cash=Fraction(1000))
    assert short_account.open(-1, Fraction(1), notional)
    assert short_account.apply_funding(rate, notional) == Fraction(1, 10)
    assert short_account.cash == Fraction(1000) + Fraction(1, 10)
    short_account.close(Fraction(1), notional)


def test_funding_exact_amount_on_every_applied_settlement(tmp_path):
    quotes = _quote_rows(days=3, per_day=48)
    funding = _midpoint_funding(quotes, rate="0.000137")
    events = _write_tape(tmp_path / "exact.jsonl", quotes + funding)
    plan = prepare_control_plan(
        [
            {"ts": int(q["ts_ns"]), "bid": q["bid"], "ask": q["ask"]}
            for q in quotes
        ],
        6,
        100,
        7,
        funding=funding,
    )
    records = []
    for seed_index in range(12):
        result = run_control_seed(plan, seed_index)
        records.extend(result.funding_records)
        # The seed total is the exact sum of its applied settlements.
        assert result.funding == sum(
            (Fraction(record["amount"]) for record in result.funding_records),
            Fraction(0),
        )
        assert result.funding == sum(
            (Fraction(day["funding_exact"]) for day in result.days.values()),
            Fraction(0),
        )
    assert records
    sides = set()
    for record in records:
        rate = Fraction(record["rate"])
        reference = Fraction(record["reference_notional"])
        side = int(record["side"])
        sides.add(side)
        expected = -side * reference * rate
        assert Fraction(record["amount"]) == expected
        # Long with positive rate pays; short with positive rate receives.
        if side > 0 and rate > 0:
            assert Fraction(record["amount"]) < 0
        if side < 0 and rate > 0:
            assert Fraction(record["amount"]) > 0
        # Half-open window.
        assert int(record["entry_ts"]) <= int(record["settlement_ts"]) < int(record["exit_ts"])
    # Both directions are exercised, so the sign convention is not vacuous.
    assert sides == {1, -1}
    assert any(Fraction(record["amount"]) < 0 for record in records)
    assert any(Fraction(record["amount"]) > 0 for record in records)


def test_funding_changes_net_pnl_but_not_selection(tmp_path):
    quotes = _quote_rows(days=3, per_day=48)
    funding = _midpoint_funding(quotes)
    events = _tape(tmp_path, funding=[])
    plain = prepare_control_plan(
        [{"ts": int(q["ts_ns"]), "bid": q["bid"], "ask": q["ask"]} for q in quotes],
        6,
        100,
        11,
    )
    funded = prepare_control_plan(
        [{"ts": int(q["ts_ns"]), "bid": q["bid"], "ask": q["ask"]} for q in quotes],
        6,
        100,
        11,
        funding=funding,
    )
    # Selection is cost-model independent.
    assert plain.selection_plan_hash == funded.selection_plan_hash
    assert plain.plan_hash != funded.plan_hash
    for seed_index in range(8):
        assert (
            run_control_seed(plain, seed_index).selection_hash
            == run_control_seed(funded, seed_index).selection_hash
        )
    # Funding does move realised PnL and the control identity.
    plain_result = run_control_seed(plain, 0)
    funded_result = run_control_seed(funded, 0)
    assert funded_result.funding != 0
    assert funded_result.net_pnl_total == plain_result.net_pnl_total + funded_result.funding
    assert funded_result.control_hash != plain_result.control_hash
    assert events


# ---------------------------------------------------------------------------
# Window boundaries, dedupe and no future funding
# ---------------------------------------------------------------------------


def test_funding_window_is_half_open_at_entry_and_exit(tmp_path):
    ts = [100, 200, 300, 400]
    assert funding_window_indices(ts, 200, 400) == (1, 3)
    assert funding_window_indices(ts, 150, 450) == (1, 4)
    # A settlement exactly at entry applies; exactly at exit does not.
    assert funding_window_indices(ts, 100, 200) == (0, 1)
    assert funding_window_indices(ts, 300, 400) == (2, 3)
    assert funding_window_indices(ts, 400, 500) == (3, 4)

    # End-to-end: place a settlement at one observed entry and one observed
    # exit.  The entry settlement is applied, the exit settlement is not.
    quotes = _quote_rows(days=3, per_day=48)
    probe_funding = _midpoint_funding(quotes)
    base_plan = prepare_control_plan(
        [{"ts": int(q["ts_ns"]), "bid": q["bid"], "ask": q["ask"]} for q in quotes],
        1,
        100,
        5,
        funding=probe_funding,
    )
    baseline = run_control_seed(base_plan, 0)
    assert baseline.funding_records
    record = baseline.funding_records[0]

    at_entry = [{"ts_ns": record["entry_ts"], "rate": record["rate"]}]
    at_exit = [{"ts_ns": record["exit_ts"], "rate": record["rate"]}]
    entry_plan = prepare_control_plan(
        [{"ts": int(q["ts_ns"]), "bid": q["bid"], "ask": q["ask"]} for q in quotes],
        1,
        100,
        5,
        funding=at_entry,
    )
    exit_plan = prepare_control_plan(
        [{"ts": int(q["ts_ns"]), "bid": q["bid"], "ask": q["ask"]} for q in quotes],
        1,
        100,
        5,
        funding=at_exit,
    )
    entry_result = run_control_seed(entry_plan, 0)
    exit_result = run_control_seed(exit_plan, 0)
    assert [r["settlement_ts"] for r in entry_result.funding_records] == [
        int(record["entry_ts"])
    ]
    assert exit_result.funding_records == ()
    # Removing every settlement returns the unfunded PnL exactly.
    assert exit_result.net_pnl_total == baseline.net_pnl_total - baseline.funding
    # Keeping only the entry settlement adds exactly that one amount.
    assert entry_result.net_pnl_total == (
        baseline.net_pnl_total - baseline.funding + Fraction(record["amount"])
    )


def test_duplicate_funding_events_are_deduped_by_ts_and_rate(tmp_path):
    quotes = _quote_rows(days=1, per_day=24)
    ts = int(quotes[5]["ts_ns"])
    duplicates = [_funding_row(ts, "0.00004113") for _ in range(7)]
    distinct = [_funding_row(ts, "-0.00003432")]
    events = _write_tape(tmp_path / "dup.jsonl", quotes + duplicates + distinct)

    settlements, raw = load_funding(events)
    assert raw == 8
    assert len(settlements) == 2
    assert {(s.ts, s.rate) for s in settlements} == {
        (ts, Fraction("0.00004113")),
        (ts, Fraction("-0.00003432")),
    }
    ordered, raw_again = normalize_funding(
        duplicates + distinct + [_funding_row(ts - 10, "0.0001")]
    )
    assert raw_again == 9
    assert [s.ts for s in ordered] == [ts - 10, ts, ts]

    report = matched_controls(
        events,
        trades=2,
        notional=100,
        seed=3,
        replications=2,
    )
    funding_block = report["funding"]
    assert funding_block["raw_events"] == 8
    assert funding_block["unique_settlements"] == 2
    assert funding_block["duplicates_dropped"] == 6
    assert funding_block["sign_probe"]["passed"] is True


def test_no_future_funding_can_change_a_run(tmp_path):
    quotes = _quote_rows(days=3, per_day=48)
    known = _midpoint_funding(quotes[:144], rate="0.0002")
    future = [
        _funding_row(int(quotes[-1]["ts_ns"]) + DAY_NS, "0.01"),
        _funding_row(int(quotes[-1]["ts_ns"]) + 10 * DAY_NS, "-0.02"),
    ]
    base_plan = prepare_control_plan(
        [{"ts": int(q["ts_ns"]), "bid": q["bid"], "ask": q["ask"]} for q in quotes],
        6,
        100,
        9,
        funding=known,
    )
    future_plan = prepare_control_plan(
        [{"ts": int(q["ts_ns"]), "bid": q["bid"], "ask": q["ask"]} for q in quotes],
        6,
        100,
        9,
        funding=known + future,
    )
    for seed_index in range(6):
        base = run_control_seed(base_plan, seed_index)
        with_future = run_control_seed(future_plan, seed_index)
        assert base.funding == with_future.funding
        assert base.net_pnl_total == with_future.net_pnl_total
        assert base.funding_records == with_future.funding_records
        for record in with_future.funding_records:
            assert int(record["settlement_ts"]) < int(record["exit_ts"])
            assert int(record["settlement_ts"]) <= int(quotes[-1]["ts_ns"])


# ---------------------------------------------------------------------------
# Zero-trade day, missing mark and report diagnostics
# ---------------------------------------------------------------------------


def test_zero_trade_day_retained_with_funding(tmp_path):
    quotes = _quote_rows(days=3, per_day=48)
    funding = _midpoint_funding(quotes)
    events = _write_tape(tmp_path / "zero.jsonl", quotes + funding)
    day_one_end = int((START + datetime.timedelta(days=1)).timestamp() * 1_000_000_000) - 1
    report = matched_controls(
        events,
        trades=4,
        notional=100,
        seed=5,
        replications=4,
        train_end_ns=day_one_end,
    )
    series = {row["day"]: row for row in report["daily_series"]}
    assert len(series) == 3
    first_day = min(series)
    assert series[first_day]["trades"] == 0
    assert series[first_day]["funding"] == "0"
    assert series[first_day]["zero_trade_seeds"] == 4
    assert Fraction(report["funding"]["total_funding_exact"]) == sum(
        (Fraction(row["funding"]) for row in series.values()), Fraction(0)
    )


def test_missing_mark_is_not_filled_and_funding_is_not_invented(tmp_path):
    quotes = _quote_rows(days=3, per_day=48)
    # Corrupt every evaluation-window quote on the last day so any exit mark is
    # unusable.  The day must stay MISSING_MARK, never zero-filled.
    last_day_start = int((START + datetime.timedelta(days=2)).timestamp() * 1_000_000_000)
    rows = []
    for row in quotes:
        row = dict(row)
        if int(row["ts_ns"]) >= last_day_start:
            row["bid"] = "101.00"
            row["ask"] = "100.00"
        rows.append(row)
    funding = _midpoint_funding(quotes)
    events = _write_tape(tmp_path / "missing.jsonl", rows + funding)
    report = matched_controls(events, trades=4, notional=100, seed=8, replications=3)
    missing = [row for row in report["daily_series"] if row["missing_seeds"] > 0]
    assert missing
    for row in missing:
        assert row["mean_net_pnl"] is None
        assert row["mean_net_pnl_float"] is None
    assert report["diagnostics"]["missing_mark_seeds"] >= 1


def test_report_exposes_funding_per_day_and_per_stratum(tmp_path):
    events = _tape(tmp_path)
    report = matched_controls(events, trades=6, notional=100, seed=13, replications=6)
    funding_block = report["funding"]
    assert funding_block["model"] == "EXACT_NATIVE_TAPE_FUNDING"
    assert funding_block["sign_convention"] == FUNDING_SIGN_CONVENTION
    assert funding_block["window"] == FUNDING_WINDOW
    assert funding_block["mark_inference"] is False
    assert funding_block["future_funding_applied"] is False
    assert funding_block["sign_probe"]["passed"] is True
    assert funding_block["unique_settlements"] > 0

    total = Fraction(funding_block["total_funding_exact"])
    assert total != 0
    day_total = sum(
        (Fraction(row["funding"]) for row in report["daily_series"] if row["funding"] is not None),
        Fraction(0),
    )
    assert day_total == total
    stratum_total = sum(
        (Fraction(row["funding"]) for row in report["per_stratum"]), Fraction(0)
    )
    assert stratum_total == total
    for row in report["per_stratum"]:
        assert "funding" in row and "mean_funding" in row
        assert Fraction(row["funding"]) != 0 or row["trades"] == 0
    for row in report["daily_series"]:
        assert "funding" in row and "mean_funding" in row


# ---------------------------------------------------------------------------
# Batch/sequential subset, trials and costs
# ---------------------------------------------------------------------------


def test_batch_and_sequential_subset_match_with_funding(tmp_path):
    events = _tape(tmp_path, name="subset.jsonl")
    report = matched_controls(
        events,
        trades=6,
        notional=100,
        seed=17,
        replications=12,
        compare_executions=True,
        comparison_replications=5,
    )
    comparison = report["batch_vs_sequential"]
    assert comparison["subset_seeds"] == 5
    assert comparison["full_replications"] == 12
    assert comparison["full_sequential_run"] is False
    assert comparison["batch_completed"] == comparison["sequential_completed"] == 5
    assert comparison["series_equal"] is True
    assert comparison["seed_series_equal"] is True
    assert comparison["equal"] is True
    assert comparison["batch_series_hash"] == comparison["sequential_series_hash"]
    assert comparison["batch_seed_series_hash"] == comparison["sequential_seed_series_hash"]
    assert "not claimed" in comparison["note"]
    # The full batch run itself is complete for its 12 seeds.
    assert report["seeds_completed"] == 12


def test_financial_trials_and_cost_reservations_with_funding(tmp_path):
    events = _tape(tmp_path, name="trials.jsonl")
    trials = [
        {"trial_id": "T-F1", "status": "EXECUTED", "reservation": "100"},
        {"trial_id": "T-F2", "status": "FAILED", "reservation": "100"},
    ]
    report = matched_controls(
        events,
        trades=5,
        notional=100,
        seed=19,
        replications=4,
        financial_trials=trials,
    )
    ledger = report["financial_trials"]
    assert ledger["source"] == "CALLER_SUPPLIED_PRESERVED"
    assert ledger["preserved_count"] == ledger["input_count"] == 2
    assert ledger["dropped"] == 0
    assert ledger["entries"] == trials

    derived = matched_controls(events, trades=5, notional=100, seed=19, replications=4)
    ledger = derived["financial_trials"]
    assert ledger["source"] == "DERIVED_ONE_PER_CONTROL_SEED"
    assert ledger["preserved_count"] == 4
    assert ledger["dropped"] == 0
    assert all("funding" in entry for entry in ledger["entries"])
    assert sum(
        (Fraction(entry["funding"]) for entry in ledger["entries"]), Fraction(0)
    ) == Fraction(derived["funding"]["total_funding_exact"])

    costs = derived["cost_reservations"]["derived"]
    assert "funding_reserve_bound" in costs
    assert "funding_rate_abs_sum_in_eval" in costs
    assert Fraction(costs["funding_rate_abs_sum_in_eval"]) > 0


def test_daily_diagnostics_keep_trade_counts_with_funding(tmp_path):
    events = _tape(tmp_path, name="diag.jsonl")
    funded = matched_controls(events, trades=6, notional=100, seed=23, replications=6)
    no_funding = matched_controls(
        events, trades=6, notional=100, seed=23, replications=6, funding=[]
    )
    # Funding changes PnL, never the matched schedule.
    assert _quote_fingerprint(funded) == _quote_fingerprint(no_funding)
    assert [c["trades"] for c in funded["controls"]] == [
        c["trades"] for c in no_funding["controls"]
    ]
    assert funded["funding"]["total_funding_exact"] != "0"
    assert no_funding["funding"]["unique_settlements"] == 0
    assert funded["diagnostics"]["executed_trades"] == no_funding["diagnostics"][
        "executed_trades"
    ]
    assert funded["diagnostics"]["rejected_orders"] == no_funding["diagnostics"][
        "rejected_orders"
    ]
    for row in funded["daily_series"]:
        assert row["target_trades"] == no_funding["daily_series"][
            funded["daily_series"].index(row)
        ]["target_trades"]
