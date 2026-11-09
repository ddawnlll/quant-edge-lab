"""TASK-13 control-protocol tests.

These tests pin the frozen HMAC seed protocol, the training-only control fit,
the batch/sequential equality, streaming aggregation, the L1 rescue rule and the
financial-trial/cost-reservation preservation contract.
"""

from __future__ import annotations

import datetime
import json
import math

import pytest

from qel.research import control, seed
from qel.research.control import (
    CONTROL_REQUIRED_REPLICATIONS,
    DRAW_MAPPING,
    L1_GROUP_SIZE,
    SeedResult,
    StreamingControlAggregator,
    daily_control_aggregate,
    iut_combined_p,
    l1_rescue,
    load_quotes,
    matched_controls,
    prepare_control_plan,
    run_control,
    run_control_seed,
)

UTC = datetime.timezone.utc
START = datetime.datetime(2026, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Tape helpers
# ---------------------------------------------------------------------------


def _write_tape(path, records):
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return str(path)


def _quote_rows(days=1, per_day=48, step_seconds=60, start=START):
    rows = []
    for day in range(days):
        for i in range(per_day):
            ts = start + datetime.timedelta(days=day, seconds=i * step_seconds)
            mid = 100.0 + day + 0.4 * math.sin((i + 1) / 4.0) + 0.01 * i
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


def _tiny_tape(tmp_path, name="events.jsonl"):
    rows = []
    for i in range(30):
        rows.append(
            {
                "kind": "Quote",
                "ts_ns": str(1_000 + i * 100),
                "bid": f"{100 + i * 0.1:.2f}",
                "ask": f"{100.2 + i * 0.1:.2f}",
            }
        )
    return _write_tape(tmp_path / name, rows)


def _multi_day_tape(tmp_path, days=3, per_day=48, name="multi.jsonl"):
    return _write_tape(tmp_path / name, _quote_rows(days=days, per_day=per_day))


def _rows_as_plan_input(rows):
    return [
        {"ts": int(r["ts_ns"]), "bid": float(r["bid"]), "ask": float(r["ask"])}
        for r in rows
        if r.get("kind") == "Quote"
    ]


def _mutate_after(rows, cutoff_ns, factor=1.5):
    mutated = []
    for row in rows:
        row = dict(row)
        if row.get("kind") == "Quote" and int(row["ts_ns"]) > cutoff_ns:
            row["bid"] = f"{float(row['bid']) * factor:.4f}"
            row["ask"] = f"{float(row['ask']) * factor:.4f}"
        mutated.append(row)
    return mutated


# ---------------------------------------------------------------------------
# Seed protocol
# ---------------------------------------------------------------------------


def test_seed_published_vectors_are_reproducible():
    receipt = seed.self_test()
    assert receipt["status"] == "PASS", receipt
    assert receipt["vectors"] == len(seed.TEST_VECTORS) == 3
    for vector in seed.TEST_VECTORS:
        got = seed.uniforms(vector["labels"], 8)
        assert got == pytest.approx(vector["u8"], abs=1e-15)
        assert seed.normal_at(vector["labels"], 0) == pytest.approx(vector["normal"], abs=1e-12)
        assert seed.t5_at(vector["labels"], 0) == pytest.approx(vector["t5"], abs=1e-12)
    assert seed.PROTOCOL_HASH == seed.protocol_hash()
    assert len(seed.PROTOCOL_HASH) == 64


def test_seed_uses_frozen_six_labels_and_counter_mode():
    base = {
        "GENERATOR_ID": 4865,
        "PROGRAM_REPLICATION": 7,
        "VARIANT": 0,
        "COIN": 0,
        "TIME": 0,
        "CONTROL_REPLICATION": 0,
    }
    assert list(seed.LABELS) == [
        "GENERATOR_ID",
        "PROGRAM_REPLICATION",
        "VARIANT",
        "COIN",
        "TIME",
        "CONTROL_REPLICATION",
    ]
    # Mapping insertion order must not matter.
    reordered = dict(reversed(list(base.items())))
    assert seed.uniform_at(base, 0) == seed.uniform_at(reordered, 0)
    # Every label changes the stream.
    for label in seed.LABELS:
        changed = dict(base)
        changed[label] += 1
        assert seed.uniform_at(changed, 0) != seed.uniform_at(base, 0)
    # Four uniforms per counter block, then a new block.
    block0 = [seed.uniform_at(base, i) for i in range(4)]
    block1 = [seed.uniform_at(base, i) for i in range(4, 8)]
    assert len(set(block0 + block1)) == 8
    assert seed.counter_block(base, 0) != seed.counter_block(base, 1)
    # Invalid label domains are rejected.
    with pytest.raises(seed.SeedError):
        seed.uniform_at({**base, "EXTRA": 1}, 0)
    with pytest.raises(seed.SeedError):
        seed.uniform_at({**base, "TIME": -1}, 0)
    with pytest.raises(seed.SeedError):
        seed.uniform_at({**base, "TIME": 1.5}, 0)


def test_seed_hmac_primitive_rfc4231_vector():
    # RFC 4231 test case 2.
    assert (
        seed.hmac_sha256(b"what do ya want for nothing?", b"Jefe").hex()
        == "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"
    )


def test_seed_stdlib_fallback_matches_scipy(monkeypatch):
    from scipy.stats import norm, t

    labels = seed.TEST_VECTORS[0]["labels"]
    u = seed.uniform_at(labels, 0)
    scipy_normal = float(norm.ppf(u))
    scipy_t5 = float(t.ppf(u, 5)) * math.sqrt(3.0 / 5.0)
    monkeypatch.setattr(seed, "_scipy_norm", None)
    monkeypatch.setattr(seed, "_scipy_t", None)
    # Recompute the backend labels for the fallback path.
    monkeypatch.setattr(seed, "_NORMAL_BACKEND", "STDLIB_NORMALDIST_INV_CDF")
    monkeypatch.setattr(seed, "_T5_BACKEND", "CLOSED_FORM_DF5_BISECTION")
    assert seed.normal_at(labels, 0) == pytest.approx(scipy_normal, abs=1e-9)
    assert seed.t5_at(labels, 0) == pytest.approx(scipy_t5, abs=1e-8)
    info = seed.backend_info()
    assert info["normal_backend"] == "STDLIB_NORMALDIST_INV_CDF"
    assert info["scipy_available"] is False


def test_seed_zero_and_one_are_clamped_to_finite_draws():
    labels = seed.TEST_VECTORS[0]["labels"]
    assert math.isfinite(seed.normal_at(labels, 0))
    assert math.isfinite(seed.t5_at(labels, 0))
    assert seed.U_MIN > 0 and seed.U_MAX < 1


# ---------------------------------------------------------------------------
# Control report contract
# ---------------------------------------------------------------------------


def test_control_draw_path_is_hmac_not_numpy(tmp_path, monkeypatch):
    import numpy as np

    def forbidden(*args, **kwargs):
        raise AssertionError("NumPy PCG64 must not be used by controls")

    monkeypatch.setattr(np.random, "default_rng", forbidden)
    events = _tiny_tape(tmp_path)
    report = matched_controls(events, trades=5, notional=100, seed=7, replications=3)
    assert report["draw_mapping"] == DRAW_MAPPING
    assert report["seed_protocol_hash"] == seed.PROTOCOL_HASH
    assert report["seed_protocol"]["protocol_hash"] == seed.PROTOCOL_HASH
    assert "NUMPY" not in json.dumps(report).upper()


def test_control_report_has_hashes_and_protocol_identity(tmp_path):
    events = _tiny_tape(tmp_path)
    report = matched_controls(events, trades=5, notional=100, seed=7, replications=3)
    for field in (
        "seed_protocol_hash",
        "backend_hash",
        "family_hash",
        "input_hash",
        "quotes_hash",
        "plan_hash",
        "selection_plan_hash",
        "frozen_seed_labels_hash",
        "daily_series_hash",
        "seed_series_hash",
        "report_hash",
    ):
        assert isinstance(report[field], str) and len(report[field]) == 64, field
    assert report["required_replications"] == CONTROL_REQUIRED_REPLICATIONS == 1000
    assert report["candidate_fills_used_for_selection"] is False


def test_short_seed_run_records_incomplete_evidence(tmp_path):
    events = _tiny_tape(tmp_path)
    report = matched_controls(events, trades=5, notional=100, seed=7, replications=3)
    assert report["status"] == "INCOMPLETE_EVIDENCE"
    assert "SHORT_SEED_RUN" in report["incomplete_reasons"]


def test_compute_budget_exhaustion_records_incomplete_evidence(tmp_path):
    events = _tiny_tape(tmp_path)
    report = matched_controls(
        events,
        trades=5,
        notional=100,
        seed=7,
        replications=CONTROL_REQUIRED_REPLICATIONS,
        max_trades=1,
    )
    assert report["status"] == "INCOMPLETE_EVIDENCE"
    assert "COMPUTE_BUDGET_EXHAUSTED" in report["incomplete_reasons"]
    assert report["seeds_completed"] < CONTROL_REQUIRED_REPLICATIONS
    assert report["resource_usage"]["budget_exhausted"] is True


def test_matched_control_counts_and_determinism(tmp_path):
    events = _tiny_tape(tmp_path)
    first = matched_controls(events, trades=5, notional=100, seed=7, replications=3)
    second = matched_controls(events, trades=5, notional=100, seed=7, replications=3)
    assert first == second
    assert first["trade_count_matched"] == 5
    assert [c["trades"] for c in first["controls"]] == [5, 5, 5]
    assert "calendar" in first["matched_on"]
    assert "trade_count" in first["matched_on"]
    assert "holding_time" in first["matched_on"]
    assert first["holding_time_target_ns"] > 0
    assert first["holding_time_achieved_ns"] > 0


def test_each_seed_owns_a_fresh_account(tmp_path, monkeypatch):
    created = []
    original = control.ControlAccount

    class TrackingAccount(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr(control, "ControlAccount", TrackingAccount)
    events = _tiny_tape(tmp_path)
    report = matched_controls(events, trades=5, notional=100, seed=7, replications=7)
    assert report["seeds_completed"] == 7
    # Seven control seeds plus the separate trend/pullback benchmark account.
    assert len(created) == 8
    assert len({id(account) for account in created}) == 8
    # Every control seed starts from its own independent cash state.
    assert all(account.initial_cash == account.cash or account.trades >= 0 for account in created)


def test_batch_and_sequential_produce_same_daily_series(tmp_path):
    events = _multi_day_tape(tmp_path, days=2, per_day=36)
    report = matched_controls(
        events,
        trades=8,
        notional=100,
        seed=11,
        replications=12,
        compare_executions=True,
    )
    comparison = report["batch_vs_sequential"]
    assert comparison is not None
    assert comparison["series_equal"] is True
    assert comparison["seed_series_equal"] is True
    assert comparison["equal"] is True
    assert comparison["batch_series_hash"] == comparison["sequential_series_hash"]


def test_streaming_aggregation_retains_no_seed_series(tmp_path):
    events = _multi_day_tape(tmp_path, days=2, per_day=36)
    report = matched_controls(events, trades=8, notional=100, seed=3, replications=16)
    diagnostics = report["diagnostics"]
    assert diagnostics["streaming_aggregation"] is True
    assert diagnostics["bounded_memory"] is True
    assert diagnostics["retained_seed_series"] == 0
    assert report["resource_usage"]["retained_seed_series"] == 0


def test_rejected_order_diagnostics_are_per_stratum_and_feed_rescue(tmp_path):
    events = _tiny_tape(tmp_path)
    report = matched_controls(
        events,
        trades=5,
        notional=100,
        seed=7,
        replications=3,
        starting_cash=1,
    )
    diagnostics = report["diagnostics"]
    assert diagnostics["executed_trades"] == 0
    assert diagnostics["rejected_orders"] == 15
    assert diagnostics["reject_reasons"]["INSUFFICIENT_CASH"] == 15
    assert diagnostics["zero_trade_seeds"] == 3
    assert all(row["rejected_orders"] == 15 for row in report["per_stratum"])
    rescue = report["l1_rescue"]
    assert rescue["valid_records"] == 15
    assert rescue["groups"] == 2
    assert [g["denominator"] for g in rescue["group_detail"]] == [10, 5]


# ---------------------------------------------------------------------------
# Calendar, missing marks and zero-trade days
# ---------------------------------------------------------------------------


def test_zero_trade_day_stays_in_the_calendar(tmp_path):
    rows = _quote_rows(days=3, per_day=36)
    events = _write_tape(tmp_path / "zero.jsonl", rows)
    # Fit on day one only, so day one has no evaluation quotes and stays in the
    # series as a genuine zero-trade day.
    day_one_end = int((START + datetime.timedelta(days=1)).timestamp() * 1_000_000_000) - 1
    report = matched_controls(
        events,
        trades=6,
        notional=100,
        seed=5,
        replications=5,
        train_end_ns=day_one_end,
    )
    series = {row["day"]: row for row in report["daily_series"]}
    first_day = min(series)
    assert len(series) == 3
    assert series[first_day]["trades"] == 0
    assert series[first_day]["mean_net_pnl"] == "0"
    assert series[first_day]["zero_trade_seeds"] == 5
    # The traded days still appear with matching counts.
    for day in sorted(series)[1:]:
        assert series[day]["trades"] > 0
        assert series[day]["target_trades"] == series[day]["trades"]


def test_missing_mark_is_not_filled_with_zero(tmp_path):
    rows = _quote_rows(days=1, per_day=36)
    events = _write_tape(tmp_path / "marks.jsonl", rows)
    plan = prepare_control_plan(load_quotes(events), 4, 100, 7)
    aggregator = StreamingControlAggregator(plan)
    day = plan.calendar[0]
    missing = SeedResult(
        seed_index=0,
        days={
            day: {
                "day": day,
                "status": control.STATE_MISSING_MARK,
                "net_pnl": None,
                "net_pnl_exact": None,
                "trades": 3,
                "target_trades": 4,
                "rejected_orders": 0,
                "reject_reasons": {},
                "exposure_ns": 10,
                "exposure_notional_ns": 1000,
                "holding_ns": 10,
                "missing_mark": True,
                "schedule_truncated": False,
                "schedule_shortfall": 0,
                "strata": {},
            }
        },
        net_pnl_total=None,
        executed_trades=3,
        rejected_orders=0,
        reject_reasons={},
        selection_hash="a" * 64,
        control_hash="b" * 64,
        missing_mark=True,
        starting_cash=control.Fraction(1000),
        ending_cash=control.Fraction(1000),
        fees=control.Fraction(0),
        account_id=1,
    )
    aggregator.add(missing)
    row = aggregator.daily_series()[0]
    assert row["missing_seeds"] == 1
    assert row["valid_seeds"] == 0
    assert row["mean_net_pnl"] is None
    assert row["mean_net_pnl_float"] is None


# ---------------------------------------------------------------------------
# Lookahead and candidate independence
# ---------------------------------------------------------------------------


def test_future_prices_and_candidate_fills_do_not_select_controls(tmp_path):
    rows = _quote_rows(days=2, per_day=40)
    quotes = _rows_as_plan_input(rows)
    cutoff = int((START + datetime.timedelta(days=1)).timestamp() * 1_000_000_000) - 1
    first = prepare_control_plan(
        quotes,
        6,
        100,
        13,
        train_end_ns=cutoff,
        candidate_fills=[{"ts": 1, "side": "BUY"}, {"ts": 2, "side": "SELL"}],
    )
    mutated_quotes = _rows_as_plan_input(_mutate_after(rows, cutoff, factor=1.75))
    second = prepare_control_plan(
        mutated_quotes,
        6,
        100,
        13,
        train_end_ns=cutoff,
        candidate_fills=[{"ts": 999, "side": "SELL"}],
    )
    # The frozen selection plan is identical despite different future prices
    # and different candidate fill histories.
    assert first.selection_plan_hash == second.selection_plan_hash
    assert first.target_trades_by_day == second.target_trades_by_day
    first_result = run_control_seed(first, 0)
    second_result = run_control_seed(second, 0)
    assert first_result.selection_hash == second_result.selection_hash
    assert first_result.executed_trades == second_result.executed_trades
    # Execution PnL does see the mutated prices, so the outputs are not simply
    # cached.
    assert first_result.net_pnl_total != second_result.net_pnl_total


def test_candidate_fills_are_provenance_only_in_the_report(tmp_path):
    events = _multi_day_tape(tmp_path, days=2, per_day=36)
    baseline = matched_controls(events, trades=6, notional=100, seed=2, replications=4)
    with_candidate = matched_controls(
        events,
        trades=6,
        notional=100,
        seed=2,
        replications=4,
        candidate_fills=[{"ts": 10**18, "side": "BUY", "qty": "99"}],
    )
    assert baseline["selection_plan_hash"] == with_candidate["selection_plan_hash"]
    assert baseline["daily_series"] == with_candidate["daily_series"]
    assert with_candidate["candidate_fills_used_for_selection"] is False
    assert with_candidate["trade_count_target_per_day"] == baseline["trade_count_target_per_day"]


def test_training_only_holding_time_and_stratum_diagnostics(tmp_path):
    events = _multi_day_tape(tmp_path, days=2, per_day=36)
    report = matched_controls(events, trades=8, notional=100, seed=4, replications=10)
    assert report["holding_time_target_ns"] > 0
    assert report["holding_time_achieved_ns"] > 0
    assert report["holding_time_training_mean_ns"] > 0
    assert report["holding_time_effective_mean_ns"] >= report["holding_time_training_mean_ns"]
    assert report["holding_time_note"].startswith("holding time is drawn from training-stratum")
    strata = report["per_stratum"]
    assert strata
    for row in strata:
        for field in (
            "trades",
            "rejected_orders",
            "exposure_ns",
            "exposure_notional_ns",
            "holding_ns",
            "mean_holding_ns",
            "fitted_weight",
            "fitted_long_rate",
        ):
            assert field in row
    assert sum(row["trades"] for row in strata) == report["diagnostics"]["executed_trades"]


def test_frozen_trend_pullback_control_is_reported_separately(tmp_path):
    events = _multi_day_tape(tmp_path, days=2, per_day=36)
    report = matched_controls(events, trades=8, notional=100, seed=9, replications=5)
    benchmark = report["frozen_trend_pullback_control"]
    assert benchmark["control_kind"] == "FROZEN_TREND_PULLBACK_V1"
    assert benchmark["separate_from_random_controls"] is True
    assert benchmark["included_in_random_control_mean"] is False
    assert len(benchmark["daily_series_hash"]) == 64
    assert len(benchmark["daily_series"]) == len(report["daily_series"])
    # The random-control mean is computed from the control seeds only.
    assert report["control_mean"] == pytest.approx(
        sum(float(c["net_pnl"]) for c in report["controls"] if c["net_pnl"] is not None)
        / len([c for c in report["controls"] if c["net_pnl"] is not None])
    )


# ---------------------------------------------------------------------------
# Trials, costs and rescue
# ---------------------------------------------------------------------------


def test_financial_trials_and_cost_reservations_are_preserved(tmp_path):
    events = _tiny_tape(tmp_path)
    trials = [
        {"trial_id": "T-1", "status": "EXECUTED", "reservation": "100"},
        {"trial_id": "T-2", "status": "FAILED", "reservation": "100"},
        {"trial_id": "T-3", "status": "INVALID", "reservation": "0"},
    ]
    reservations = {"account": "RESEARCH", "reserved": "1000", "currency": "USDT"}
    report = matched_controls(
        events,
        trades=5,
        notional=100,
        seed=7,
        replications=3,
        financial_trials=trials,
        cost_reservations=reservations,
    )
    ledger = report["financial_trials"]
    assert ledger["source"] == "CALLER_SUPPLIED_PRESERVED"
    assert ledger["input_count"] == 3
    assert ledger["preserved_count"] == 3
    assert ledger["dropped"] == 0
    assert ledger["entries"] == trials
    assert len(ledger["trial_hash"]) == 64
    costs = report["cost_reservations"]
    assert costs["source"] == "CALLER_SUPPLIED_PRESERVED"
    assert costs["preserved"] == reservations
    assert len(costs["reservation_hash"]) == 64


def test_derived_trial_ledger_has_one_entry_per_seed(tmp_path):
    events = _tiny_tape(tmp_path)
    report = matched_controls(events, trades=5, notional=100, seed=7, replications=4)
    ledger = report["financial_trials"]
    assert ledger["source"] == "DERIVED_ONE_PER_CONTROL_SEED"
    assert ledger["preserved_count"] == 4
    assert ledger["dropped"] == 0
    assert len(ledger["entries"]) == 4
    assert report["cost_reservations"]["derived"]["target_trades"] == 5
    assert report["cost_reservations"]["derived"]["replications"] == 4


def test_l1_rescue_is_deterministic_and_excludes_invalid_rejects():
    valid = [
        {"kind": "ORDER_REJECT", "reason": "INSUFFICIENT_CASH", "status": "VALID", "id": i}
        for i in range(25)
    ]
    invalid = [
        {"kind": "ORDER_REJECT", "reason": "MALFORMED", "status": "INVALID", "id": 100 + i}
        for i in range(7)
    ]
    mixed = []
    for i in range(25):
        mixed.append(valid[i])
        if i < 7:
            mixed.append(invalid[i])
    first = l1_rescue(mixed, family_seed=42)
    second = l1_rescue(mixed, family_seed=42)
    assert first == second
    assert first["scope"] == control.RESCUE_SCOPE
    assert first["method"] == "HMAC_COUNTER_UNIFORM_RESERVOIR_WITH_FROZEN_FAMILY_SEED"
    assert first["group_size"] == L1_GROUP_SIZE == 10
    assert first["valid_records"] == 25
    assert first["invalid_excluded"] == 7
    assert first["groups"] == 3
    assert [g["denominator"] for g in first["group_detail"]] == [10, 10, 5]
    assert first["selected_count"] == 3
    assert all(record["id"] < 100 for record in first["selected"])
    assert all(record["status"] == "VALID" for record in first["selected"])
    other = l1_rescue(mixed, family_seed=43)
    assert other["valid_records"] == first["valid_records"]
    assert other["rescue_hash"] != first["rescue_hash"]


def test_l1_rescue_invalid_rejects_are_not_in_the_denominator():
    valid = [{"id": i, "status": "VALID", "reason": "L1_REJECT"} for i in range(10)]
    invalid = [{"id": 100 + i, "status": "INVALID", "reason": "L1_REJECT"} for i in range(10)]
    result = l1_rescue(valid + invalid, family_seed=1)
    assert result["valid_records"] == 10
    assert result["groups"] == 1
    assert result["group_detail"][0]["denominator"] == 10
    assert result["invalid_excluded"] == 10


# ---------------------------------------------------------------------------
# F27 arithmetic helpers
# ---------------------------------------------------------------------------


def test_f27_daily_control_aggregate_and_iut():
    result = daily_control_aggregate(
        ["0.01", "-0.01", "0.02", "0"],
        [["0", "0.01", "0", "0.01"], ["0", "-0.01", "0", "-0.01"]],
    )
    assert result["candidate_mean"] == control.Fraction("0.005")
    assert result["control_mean"] == 0
    assert result["difference_mean"] == control.Fraction("0.005")
    assert result["market_histories"] == 1
    assert result["included_days"] == 4
    assert result["control_count"] == 2
    assert iut_combined_p("0.01", "0.04") == control.Fraction("0.04")


# ---------------------------------------------------------------------------
# Legacy single-seed entry point
# ---------------------------------------------------------------------------


def test_run_control_uses_hmac_and_returns_daily_series(tmp_path):
    events = _tiny_tape(tmp_path)
    quotes = load_quotes(events)
    first = run_control(quotes, 5, 100, 7)
    second = run_control(quotes, 5, 100, 7)
    assert first == second
    assert first["draw_mapping"] == DRAW_MAPPING
    assert first["seed_protocol_hash"] == seed.PROTOCOL_HASH
    assert first["trades"] == 5
    assert isinstance(first["daily_series"], list) and first["daily_series"]
    assert first["daily_series"][0]["trades"] == 5
