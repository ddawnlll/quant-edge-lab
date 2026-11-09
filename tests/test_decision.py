"""TASK-14 end-to-end paired decision tests (F27 / F28 audit cases)."""

from __future__ import annotations

import datetime
import json
import math
from fractions import Fraction

import pytest

from qel.stats.job import run_job
from qel.stats.core import (
    DecisionError,
    family_decision,
    holm_adjusted,
    paired_decision,
    paired_endpoints,
    required_resamples,
    two_half_diagnostic,
)

AUDIT_B = 9999  # B+1 >= max(10000, ceil(20m/alpha_0)) for m=1, j=0


def _dates(n: int) -> list[str]:
    start = datetime.date(2026, 1, 1)
    return [(start + datetime.timedelta(days=i)).isoformat() for i in range(n)]


def _panel(value: float, amplitude: float, phase: float, n: int = 120, symbol: str = "BTC") -> dict:
    """Deterministic daily net PnL panel with a nonzero-variance seasonal term."""
    rows = {}
    for t, day in enumerate(_dates(n)):
        rows[day] = f"{value + amplitude * math.sin(2 * math.pi * t / 20 + phase):.12f}"
    return {symbol: rows}


def _flat(value: str, n: int = 120, symbol: str = "BTC") -> dict:
    return {symbol: {day: value for day in _dates(n)}}


def test_audit_profitable_candidate_blocked_by_stronger_control():
    """Candidate +1/day is profitable, but a +1.5/day matched control wins.

    Endpoint A may pass; endpoint B (candidate - control vs the control floor)
    must fail, IUT must fail, Holm must not reject, and the verdict must never
    be PROMOTE.
    """
    candidate = _panel(1.0, 0.1, 0.0)
    control = _panel(1.5, 0.15, 0.7)
    result = paired_decision(
        candidate, control, "1000", "0.0001", "0",
        b=AUDIT_B, seed=0,
    )

    endpoint_a = result["primary"]["endpoints"]["A"]
    endpoint_b = result["primary"]["endpoints"]["B"]

    assert endpoint_a["mean_daily_return"] == pytest.approx(0.001, abs=1e-12)
    assert endpoint_a["p_upper"] < 0.025, "endpoint A is allowed to pass"
    assert endpoint_a["negative_evidence"] is False

    assert endpoint_b["mean_daily_return"] == pytest.approx(-0.0005, abs=1e-12)
    assert endpoint_b["p_upper"] == 1.0, "a stronger control must fail the superiority test"
    assert endpoint_b["negative_evidence"] is True

    assert result["iut_p_upper"] == 1.0
    assert result["holm"]["rejected"] == [False]
    assert result["verdict"] in ("NO_PROMOTION", "INSUFFICIENT_EVIDENCE")
    assert result["verdict"] != "PROMOTE"
    assert result["verdict"] == "NO_PROMOTION"
    assert "NEGATIVE_EVIDENCE_ENDPOINT_B" in result["reason_codes"]
    assert result["budget"]["resolved"] is True


def test_promotion_requires_resolved_compute_and_both_endpoints():
    candidate = _panel(2.0, 0.1, 0.3)
    control = _panel(-0.5, 0.15, 0.7)
    result = paired_decision(candidate, control, "1000", "0.0001", "0", b=AUDIT_B, seed=0)

    assert result["verdict"] == "PROMOTE"
    assert result["holm"]["rejected"] == [True]
    assert result["budget"]["resolved"] is True
    for name in ("A", "B"):
        endpoint = result["primary"]["endpoints"][name]
        assert endpoint["p_upper"] <= 0.025
        assert endpoint["ci_lower_95"] > endpoint["floor"]
        assert endpoint["power"] >= 0.8


def test_shared_calendar_symbol_order_and_capital_denominator():
    candidate = {
        "BTC": _panel(1.0, 0.1, 0.0)["BTC"],
        "ETH": _panel(0.5, 0.05, 1.2)["BTC"],
    }
    control = _panel(-0.25, 0.15, 0.7)
    base = paired_endpoints(candidate, control, "1000", "0.0001", "0", b=240, seed=4)

    reversed_order = {"ETH": candidate["ETH"], "BTC": candidate["BTC"]}
    flipped = paired_endpoints(reversed_order, control, "1000", "0.0001", "0", b=240, seed=4)
    assert flipped["primary"] == base["primary"]
    assert flipped["calendar_hash"] == base["calendar_hash"]
    assert flipped["method_hash"] == base["method_hash"]

    days = _dates(120)
    zero_symbol = {day: "0" for day in days}
    with_zero = paired_endpoints(
        {**candidate, "SOL": zero_symbol},
        {**control, "SOL": zero_symbol},
        "1000", "0.0001", "0", b=240, seed=4,
    )
    assert with_zero["n_symbols_candidate"] == 3
    assert with_zero["zero_trade_days_candidate"] == base["zero_trade_days_candidate"]
    # A fully aligned zero-PnL symbol changes neither the series nor the test.
    assert with_zero["primary"] == base["primary"]
    assert with_zero["method_hash"] == base["method_hash"]

    decided_base = paired_decision(candidate, control, "1000", "0.0001", "0", b=200, seed=4)
    decided_with_zero = paired_decision(
        {**candidate, "SOL": zero_symbol},
        {**control, "SOL": zero_symbol},
        "1000", "0.0001", "0", b=200, seed=4,
    )
    assert decided_with_zero["primary"] == decided_base["primary"]
    assert decided_with_zero["verdict"] == decided_base["verdict"]

    # One initial-capital denominator for both endpoints.
    mean_a = base["primary"]["endpoints"]["A"]["mean_daily_return"]
    mean_b = base["primary"]["endpoints"]["B"]["mean_daily_return"]
    assert (mean_a - base["primary"]["mean_control"]) == pytest.approx(mean_b, rel=1e-12)

    doubled_capital = paired_endpoints(candidate, control, "2000", "0.0001", "0", b=240, seed=4)
    assert doubled_capital["primary"]["endpoints"]["A"]["mean_daily_return"] == pytest.approx(
        mean_a / 2.0, rel=1e-12
    )
    assert doubled_capital["primary"]["endpoints"]["B"]["mean_daily_return"] == pytest.approx(
        mean_b / 2.0, rel=1e-12
    )
    # The economic floor is an absolute daily return, so the test statistic
    # moves with capital even though the denominator is shared.
    assert doubled_capital["calendar_hash"] == base["calendar_hash"]


def test_missing_or_misaligned_observation_is_rejected():
    days = _dates(120)
    candidate = _panel(1.0, 0.1, 0.0)
    control = _panel(0.2, 0.1, 0.7)

    broken = {"BTC": dict(candidate["BTC"])}
    del broken["BTC"][days[17]]
    with pytest.raises(DecisionError) as missing:
        paired_decision(broken, control, "1000", "0.0001", "0", b=120)
    assert missing.value.code == "MISSING_DATA"

    extra_day = (datetime.date(2027, 1, 1)).isoformat()
    misaligned = {"BTC": {**control["BTC"], extra_day: "0"}}
    with pytest.raises(DecisionError) as mismatch:
        paired_decision(candidate, misaligned, "1000", "0.0001", "0", b=120)
    assert mismatch.value.code == "MISSING_DATA"

    with pytest.raises(DecisionError):
        paired_decision({}, control, "1000", "0.0001", "0", b=120)


def test_zero_trade_days_are_retained_not_dropped():
    days = _dates(120)
    rows = {day: ("0" if i % 4 == 0 else "1") for i, day in enumerate(days)}
    candidate = {"BTC": rows}
    control = _flat("0.2")
    result = paired_endpoints(candidate, control, "1000", "0.0001", "0", b=160, seed=2)

    assert result["n_days"] == 120
    assert result["zero_trade_days_candidate"] == 30
    # 90 profitable days x 1 unit / 1000 capital / 120 calendar days.
    assert result["primary"]["endpoints"]["A"]["mean_daily_return"] == pytest.approx(
        float(Fraction(90) / 1000 / 120), rel=1e-15
    )
    # Dropping zero days would report 0.001 instead of 0.00075.
    assert result["primary"]["endpoints"]["A"]["mean_daily_return"] != pytest.approx(0.001, rel=1e-9)


def test_degenerate_variance_is_insufficient_evidence_without_crash():
    result = paired_decision(_flat("1"), _flat("1.5"), "1000", "0.0001", "0", b=200)
    assert result["verdict"] == "INSUFFICIENT_EVIDENCE"
    assert result["status"] == "BLOCKED"
    assert "DEGENERATE_VARIANCE" in result["reason_codes"]
    assert result["endpoints"] is None


def test_nonstationary_regime_shift_is_blocked():
    days = _dates(120)
    shifted = {}
    for t, day in enumerate(days):
        level = 1.0 if t < 60 else 3.0
        shifted[day] = f"{level + 0.1 * math.sin(2 * math.pi * t / 20):.12f}"
    candidate = {"BTC": shifted}
    control = _panel(0.5, 0.1, 0.3)
    result = paired_decision(candidate, control, "1000", "0.0001", "0", b=200)

    assert result["verdict"] == "INSUFFICIENT_EVIDENCE"
    assert "NONSTATIONARY" in result["reason_codes"]
    assert result["stationarity"]["status"] == "FAIL"
    assert result["stationarity"]["candidate"]["statistic"] > 3.29
    assert result["endpoints"] is None


def test_low_power_with_small_b_is_insufficient_evidence():
    # Mean daily return sits exactly on the economic floor; the effect is tiny
    # relative to the dependent noise and the compute envelope is small.
    candidate = _panel(0.1, 0.5, 0.0)
    control = _panel(0.1, 0.5, 0.9)
    result = paired_decision(candidate, control, "1000", "0.0001", "0", b=300, seed=1)

    assert result["verdict"] == "INSUFFICIENT_EVIDENCE"
    assert "LOW_POWER" in result["reason_codes"]
    assert result["primary"]["endpoints"]["A"]["power"] < 0.8
    assert result["budget"]["resolved"] is False


def test_monte_carlo_cap_returns_waiting_compute_budget():
    alpha_j = 0.05 / 2 ** 9
    result = paired_decision(
        _panel(1.0, 0.1, 0.0), _panel(0.5, 0.1, 0.7), "1000", "0.0001", "0",
        alpha=alpha_j, family_size=8, family_index=8,
        b=2000, max_resamples=1_000_000,
    )
    assert result["verdict"] == "WAITING_COMPUTE_BUDGET"
    assert result["error"] == "WAITING_COMPUTE_BUDGET"
    assert result["minimum_B"] == 1_638_399
    assert result["budget"]["resolved"] is False
    assert result["endpoints"] is None

    # Structural rejection is reported even when the budget is unresolvable.
    days = _dates(120)
    broken = {"BTC": dict(_panel(1.0, 0.1, 0.0)["BTC"])}
    del broken["BTC"][days[3]]
    with pytest.raises(DecisionError) as exc:
        paired_decision(
            broken, _panel(0.5, 0.1, 0.7), "1000", "0.0001", "0",
            alpha=alpha_j, family_size=8, family_index=8,
            b=2000, max_resamples=1_000_000,
        )
    assert exc.value.code == "MISSING_DATA"


def test_primary_sensitivity_blocks_and_budget_inputs_are_recorded():
    result = paired_endpoints(
        _panel(1.0, 0.1, 0.0), _panel(0.5, 0.1, 0.7), "1000", "0.0001", "0",
        b=200, seed=0,
    )
    assert result["primary"]["block"] == 5
    assert result["sensitivity"]["block"] == 10
    assert set(result["primary"]["endpoints"]) == {"A", "B"}
    assert set(result["sensitivity"]["endpoints"]) == {"A", "B"}
    assert result["budget"]["b_required"] == 9999
    assert result["budget"]["max_resamples"] == 1_000_000
    assert result["budget"]["alpha_schedule"] == "0.05 / 2^(j+1)"
    assert result["alpha"]["nominal"] == 0.025
    assert result["alpha"]["frozen"] == 0.025
    # Primary remains authoritative; sensitivity is reported, not substituted.
    assert "iut" in result["primary"] and "iut" in result["sensitivity"]


def test_holm_invalid_candidate_stays_in_family_with_p_one():
    days = _dates(120)
    strong = _panel(2.0, 0.1, 0.3)
    broken = {"BTC": dict(_panel(1.0, 0.1, 0.0)["BTC"])}
    del broken["BTC"][days[5]]
    control = _panel(-0.5, 0.15, 0.7)

    result = family_decision({"C1": strong, "C2": broken}, control, "1000", "0.0001", "0", b=300)

    assert result["family_size"] == 2
    assert result["candidates"]["C2"]["p_iut_upper"] == 1.0
    assert result["candidates"]["C2"]["retained_in_family"] is True
    assert result["candidates"]["C2"]["verdict"] == "INSUFFICIENT_EVIDENCE"
    assert 1.0 in result["holm"]["pvalues"]
    assert len(result["holm"]["pvalues"]) == 2
    # C1 is adjusted at the fixed m=2, not silently re-shrunk to m=1.
    assert result["candidates"]["C1"]["adjusted_p"] == pytest.approx(
        2 * result["candidates"]["C1"]["p_iut_upper"]
    )
    assert result["holm"]["rejected"][1] is False


def test_holm_adjusted_step_down_and_p_one_retention():
    table = holm_adjusted([0.001, 1.0], 0.025)
    assert table["rejected"] == [True, False]
    assert table["adjusted"][0] == pytest.approx(0.002)
    assert table["adjusted"][1] == 1.0

    stopped = holm_adjusted([0.02, 0.02], 0.025)
    assert stopped["rejected"] == [False, False]
    assert stopped["adjusted"] == pytest.approx([0.04, 0.04])


def test_required_resamples_matches_f28_mc_budget():
    assert required_resamples(1, 0) == 9999
    assert required_resamples(8, 8) == 1_638_399


def test_two_half_diagnostic_reports_insufficient_history():
    diag = two_half_diagnostic([1.0, 2.0, 1.5])
    assert diag["status"] == "INSUFFICIENT"
    assert diag["reason"] == "INSUFFICIENT_N"


def _write_audit_fixture(tmp_path):
    n = 120
    payload = {
        "candidate_daily": [
            f"{1.0 + 0.1 * math.sin(2 * math.pi * t / 20):.12f}" for t in range(n)
        ],
        "control_daily": [
            f"{1.5 + 0.15 * math.sin(2 * math.pi * t / 20 + 0.7):.12f}" for t in range(n)
        ],
        "capital": "1000",
        "economic_minimum": "0.0001",
        "control_minimum": "0",
        "power": "0.8",
        "data_boundary": "TASK-14 audit fixture",
    }
    path = tmp_path / "audit_fixture.json"
    path.write_text(json.dumps(payload))
    return path


def test_stats_job_audit_fixture_never_returns_edge_or_promote(tmp_path):
    """The production stats-job path must return the paired verdict, not EDGE."""
    result = run_job(str(_write_audit_fixture(tmp_path)))
    assert result["verdict"] in (
        "NO_PROMOTION",
        "INSUFFICIENT_EVIDENCE",
        "WAITING_COMPUTE_BUDGET",
    )
    assert result["verdict"] not in ("EDGE", "PROMOTE")
    assert result["status"] == "PASS"
    assert result["p_iut"] == 1.0
    assert result["p_control_upper"] == 1.0
    assert "IUT_NOT_REJECTED" in result["reason_codes"]
    assert result["data_boundary"] == "TASK-14 audit fixture"


def test_stats_job_audit_fixture_is_no_promotion_when_resolved(tmp_path):
    result = run_job(str(_write_audit_fixture(tmp_path)), b=AUDIT_B)
    assert result["verdict"] == "NO_PROMOTION"
    assert result["status"] == "PASS"
    assert "NEGATIVE_EVIDENCE_ENDPOINT_B" in result["reason_codes"]
    assert result["p_iut"] == 1.0
    assert result["decision_status"] == "PASS"
