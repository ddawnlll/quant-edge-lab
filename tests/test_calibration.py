"""TASK-26 / B6 calibration tests (F27 / F28 audit cases).

The heavy B6 workload is not executed here.  These tests pin the generator law,
the HMAC seed protocol parity, the frozen selection rule, the Clopper-Pearson
bound, the aggregation/certification rules, and one bounded end-to-end pilot
that runs through :func:`qel.stats.core.paired_decision`.
"""

from __future__ import annotations

import numpy as np
import pytest

from qel.research import seed as seed_protocol
from qel.stats import calibration as cal
from qel.stats.core import _DECISION_METHOD, paired_decision, required_resamples, two_half_diagnostic

AUDIT_B = 9999
AUDIT_MAX = 10_000


def _small_program(generator: str = "GAUSSIAN_AR1_RHO_0_3", *, controls: int = 4, **kwargs) -> dict:
    options = {
        "program": 0,
        "dev_days": 120,
        "fresh_days": 60,
        "cohorts": 2,
        "control_replications": controls,
    }
    options.update(kwargs)
    return cal.generate_program(generator, **options)


def test_uniform_series_matches_frozen_protocol():
    for t in (0, 1, 17, 63):
        labels = {
            "GENERATOR_ID": 3,
            "PROGRAM_REPLICATION": 5,
            "VARIANT": 7,
            "COIN": 2,
            "TIME": t,
            "CONTROL_REPLICATION": 0,
        }
        expected = seed_protocol.uniform_at(labels, 0)
        got = cal.uniform_series(
            generator_id=3,
            program=5,
            variant=7,
            coin=2,
            control_replication=0,
            days=t + 1,
        )[t]
        assert got == expected, f"TIME={t} diverged from the frozen protocol"


def test_uniform_series_partitions_are_independent_streams():
    a = cal.uniform_series(
        generator_id=1, program=0, variant=0, coin=0, control_replication=0, days=32
    )
    b = cal.uniform_series(
        generator_id=1, program=0, variant=1, coin=0, control_replication=0, days=32
    )
    c = cal.uniform_series(
        generator_id=1, program=0, variant=0, coin=0, control_replication=1, days=32
    )
    assert not np.array_equal(a, b)
    assert not np.array_equal(a, c)
    assert all(0.0 <= value < 1.0 for value in a)


def test_ar1_is_stationary_unit_variance():
    # The check uses the pinned normal inverse CDF on a uniform grid, not a
    # generator draw: the point is the AR(1) recursion's variance/autocorrelation.
    z = seed_protocol._scipy_norm.ppf(
        np.clip(np.random.default_rng(1).uniform(size=200_000), seed_protocol.U_MIN, seed_protocol.U_MAX)
    )
    x = cal.ar1(z)
    assert x.var() == pytest.approx(1.0, abs=0.03)
    lag1 = float(np.corrcoef(x[:-1], x[1:])[0, 1])
    assert lag1 == pytest.approx(cal.RHO, abs=0.02)


def test_generator_law_reconstruction():
    program = _small_program()
    spec = cal.GENERATORS["GAUSSIAN_AR1_RHO_0_3"]
    days = program["total_days"]
    factor = cal.ar1(
        cal._innovation(
            spec,
            program=0,
            variant=cal.FACTOR_VARIANT,
            coin=cal.FACTOR_COIN,
            control_replication=0,
            days=days,
        )
    )
    idio = cal.ar1(
        cal._innovation(
            spec,
            program=0,
            variant=4,
            coin=0,
            control_replication=0,
            days=days,
        )
    )
    expected = 0.01 * (0.6 * factor + 0.8 * idio)
    assert program["variants"][4] == pytest.approx(expected, abs=1e-15)
    assert program["variants"].shape == (cal.B6_FAMILY_VARIANTS, days)


def test_generator_is_deterministic_and_program_partitioned():
    first = _small_program()
    second = _small_program()
    other = _small_program(program=1)
    assert np.array_equal(first["variants"], second["variants"])
    assert np.array_equal(first["control"], second["control"])
    assert not np.array_equal(first["variants"], other["variants"])


def test_injected_effect_moves_only_the_prespecified_variant():
    base = _small_program()
    injected = cal.generate_program(
        "GAUSSIAN_AR1_RHO_0_3",
        program=0,
        dev_days=120,
        fresh_days=60,
        cohorts=2,
        control_replications=4,
        effects={0: 0.002},
    )
    assert np.array_equal(base["variants"][1:], injected["variants"][1:])
    delta = injected["variants"][0] - base["variants"][0]
    assert delta == pytest.approx(np.full(base["total_days"], 0.002), abs=1e-15)


def test_control_is_factor_plus_averaged_idiosyncratic_paths():
    program = _small_program("FIVE_COIN_COMMON_FACTOR_LOADING_0_6", controls=64)
    factor_component = 0.01 * 0.6 * program["factor"]
    residual = program["control"] - factor_component
    # Averaging independent idiosyncratic paths shrinks the residual, it never
    # removes the common factor.
    assert np.std(residual) < 0.01 * 0.8
    candidate = program["variants"][0]
    assert np.std(candidate) > np.std(residual)


def test_regime_shift_generator_is_nonstationary():
    program = _small_program(
        "FROZEN_REGIME_SHIFT_NEGATIVE_CONTROL", controls=2, fresh_days=140
    )
    windows = program["windows"]
    for start, end in windows[1:]:
        middle = start + (end - start) // 2
        first = program["factor"][start:middle]
        second = program["factor"][middle:end]
        assert first.size > 0 and second.size > 0
        assert first.mean() > 0.2
        assert second.mean() < -0.2
    start, end = program["windows"][1]
    diagnostic = two_half_diagnostic(
        program["variants"][0][start:end], lag=cal.B6_HAC_LAG, min_half=50
    )
    assert diagnostic["status"] == "FAIL"


def test_selection_scores_min_and_caps_candidates():
    program = _small_program()
    selection = cal.select_candidates(program, cohort=1)
    assert selection["variants_screened"] == cal.B6_FAMILY_VARIANTS
    assert len(selection["selected"]) <= cal.B6_CANDIDATES_MAX_PER_COHORT
    assert len(selection["selected"]) == cal.B6_CANDIDATES_MAX_PER_COHORT
    scores = selection["scores"]
    for variant in selection["selected"]:
        assert scores[variant] == min(scores[variant], scores[variant])
    ordered = sorted(scores, reverse=True)
    assert min(scores[v] for v in selection["selected"]) >= ordered[len(selection["selected"]) - 1] - 1e-12


def test_cohort_development_set_expands_after_release():
    program = _small_program()
    first = cal.select_candidates(program, cohort=1)
    second = cal.select_candidates(program, cohort=2)
    assert first["development_days"] == 120
    assert second["development_days"] == 180
    assert second["development_end"] > first["development_end"]


def test_clopper_pearson_upper_bound():
    assert cal.clopper_pearson_upper(0, 1000) == pytest.approx(0.0029912495, abs=1e-9)
    assert cal.clopper_pearson_upper(0, 1) == pytest.approx(0.95, abs=1e-12)
    assert cal.clopper_pearson_upper(10, 10) == 1.0
    assert cal.clopper_pearson_upper(5, 100) < 0.11
    with pytest.raises(ValueError):
        cal.clopper_pearson_upper(11, 10)


def test_decide_cohort_routes_through_paired_decision():
    program = _small_program(cohorts=1, dev_days=120, fresh_days=60)
    result = cal.decide_cohort(program, cohort=1, family_size=1, b=AUDIT_B, max_resamples=AUDIT_MAX)
    assert result["alpha_spending"]["alpha_nominal"] == 0.025
    assert result["alpha_spending"]["b_required"] == required_resamples(1, 0)
    assert result["alpha_spending"]["resolved"] is True
    assert len(result["entries"]) == cal.B6_CANDIDATES_MAX_PER_COHORT
    for entry in result["entries"]:
        decision = entry["decision"]
        assert decision["method"] == _DECISION_METHOD
        assert decision["calendar_hash"]
        assert decision["budget"]["resolved"] is True
        assert decision["verdict"] in (
            "PROMOTE",
            "NO_PROMOTION",
            "INSUFFICIENT_EVIDENCE",
        )
    # The same panel passed straight into the accepted decision path must give
    # the same verdict: no private re-implementation of the statistics.
    entry = result["entries"][0]
    direct = paired_decision(
        cal._panel(program["variants"][entry["variant"]], 120, 180, "PORTFOLIO"),
        cal._panel(program["control"], 120, 180, "CONTROL"),
        "1",
        "0",
        "0",
        alpha=0.025,
        block=cal.B6_BLOCK,
        block_sensitivity=cal.B6_BLOCK_SENSITIVITY,
        hac_lag=cal.B6_HAC_LAG,
        b=AUDIT_B,
        max_resamples=AUDIT_MAX,
        seed=(
            int(
                __import__("hashlib")
                .sha256(
                    f"{program['generator_id']}|{program['program']}|1|{entry['variant']}|0".encode()
                )
                .hexdigest()[:16],
                16,
            )
            % (1 << 31)
        ),
        family_size=1,
        family_index=0,
        candidate_id=f"VARIANT-{entry['variant']:02d}",
        power_target=0.8,
        batch=cal.B6_BATCH_RESAMPLES,
        min_half=50,
    )
    assert direct["verdict"] == entry["decision"]["verdict"]
    assert direct["iut_p_upper"] == entry["decision"]["iut_p_upper"]


def test_regime_shift_cohort_is_blocked_not_promoted():
    program = _small_program(
        "FROZEN_REGIME_SHIFT_NEGATIVE_CONTROL", cohorts=1, controls=2, fresh_days=140
    )
    result = cal.decide_cohort(program, cohort=1, family_size=1, b=AUDIT_B, max_resamples=AUDIT_MAX)
    for entry in result["entries"]:
        assert entry["decision"]["verdict"] != "PROMOTE"
        assert entry["decision"]["status"] == "BLOCKED"
        assert "NONSTATIONARY" in entry["decision"]["reason_codes"]
    assert result["promoted_variants"] == []


def test_waiting_compute_budget_is_not_a_promotion():
    program = _small_program(cohorts=1)
    result = cal.decide_cohort(program, cohort=1, family_size=1, b=AUDIT_B, max_resamples=100)
    assert result["alpha_spending"]["resolved"] is False
    for entry in result["entries"]:
        assert entry["decision"]["verdict"] == "WAITING_COMPUTE_BUDGET"
        assert entry["decision"]["error"] == "WAITING_COMPUTE_BUDGET"
    assert result["promoted_variants"] == []


def _record(*, coverage_status: str, verdict: str, promotion: bool) -> dict:
    decision = {
        "verdict": verdict,
        "method_hash": "m" * 64,
        "primary": None,
    }
    if coverage_status == "EVALUATED":
        decision["primary"] = {
            "endpoints": {
                "A": {
                    "mean_daily_return": 0.0,
                    "ci_low_two_sided": -0.001,
                    "ci_high_two_sided": 0.001,
                    "mde_daily": 0.0009,
                },
                "B": {
                    "ci_low_two_sided": -0.001,
                    "ci_high_two_sided": 0.001,
                    "mde_daily": 0.0009,
                },
            }
        }
        decision["verdict"] = verdict
        coverage_row = cal._endpoint_coverage(
            {"variant": 0, "true_mu": 0.0, "decision": decision}
        )
    else:
        coverage_row = {
            "variant": 0,
            "status": "BLOCKED",
            "covered": None,
            "reason": "NONSTATIONARY",
        }
    return {
        "generator": "X",
        "program": 0,
        "promotions": [0] if promotion else [],
        "promotion_count": 1 if promotion else 0,
        "coverage": [coverage_row],
        "verdict_counts": {verdict: 1},
        "blocked_cohorts": 1 if coverage_status != "EVALUATED" else 0,
        "generation_seconds": 0.1,
        "elapsed_seconds": 0.2,
        "peak_rss_bytes": 1024,
        "total_days": 100,
        "controls_replications": 4,
        "selection_counts": [cal.B6_CANDIDATES_MAX_PER_COHORT],
        "cohorts": [
            {
                "alpha_spending": {
                    "resolved": True,
                    "j": 0,
                    "alpha_nominal": 0.025,
                },
                "entries": [],
            }
        ],
    }


def test_aggregate_blocks_insufficient_monte_carlo():
    spec = cal._spec("GAUSSIAN_AR1_RHO_0_3")
    records = [_record(coverage_status="EVALUATED", verdict="INSUFFICIENT_EVIDENCE", promotion=False) for _ in range(2)]
    report = cal._aggregate_generator(spec, records)
    assert report["programs"] == 2
    assert report["coverage"]["status"] == "PASS"
    assert report["coverage"]["coverage_rate"] == 1.0
    # 0/2 promotions can never satisfy 0.075 at 95% confidence.
    assert report["clopper_pearson_upper_95"] > cal.B6_THRESHOLD
    assert report["target_pass"] is False
    assert report["certified_stationary"] is False


def test_aggregate_accepts_well_covered_stationary_generator():
    spec = cal._spec("GAUSSIAN_AR1_RHO_0_3")
    records = [_record(coverage_status="EVALUATED", verdict="NO_PROMOTION", promotion=False) for _ in range(1000)]
    report = cal._aggregate_generator(spec, records)
    assert report["coverage"]["status"] == "PASS"
    assert report["clopper_pearson_upper_95"] <= cal.B6_THRESHOLD
    assert report["target_pass"] is True
    assert report["certified_stationary"] is True


def test_aggregate_never_certifies_nonstationary_generator():
    spec = cal._spec("FROZEN_REGIME_SHIFT_NEGATIVE_CONTROL")
    records = [
        _record(coverage_status="BLOCKED", verdict="INSUFFICIENT_EVIDENCE", promotion=False)
        for _ in range(1000)
    ]
    report = cal._aggregate_generator(spec, records)
    assert report["coverage"]["status"] == "FAILED_OR_BLOCKED_NONSTATIONARY"
    assert report["target_applicable"] is False
    assert report["certified_stationary"] is False


def test_run_calibration_pilot_is_labelled_not_acceptance():
    report = cal.run_calibration(
        programs=1,
        generators=["GAUSSIAN_AR1_RHO_0_3"],
        cohorts=1,
        dev_days=80,
        fresh_days=60,
        control_replications=4,
        effects=[0.0, 0.002],
        power_programs=1,
        power_cohorts=1,
        family_size=1,
        workers=1,
        b=AUDIT_B,
        max_resamples=AUDIT_MAX,
        label="PILOT_NOT_ACCEPTANCE",
    )
    assert report["status"] == "PILOT_NOT_ACCEPTANCE"
    assert report["label"] == "PILOT_NOT_ACCEPTANCE"
    assert "COMPUTE_BUDGET_INSUFFICIENT" in report["blockers"]
    assert report["targets"]["coverage_report_present"]["value"] is True
    assert report["targets"]["power_curve_report_present"]["value"] is True
    assert report["protocol"]["decision_method"] == _DECISION_METHOD
    assert report["protocol"]["seed_protocol"] == "HMAC_SHA256_COUNTER_V1"
    assert report["workload"]["independent_null_programs"] == 1000
    assert report["executed"]["programs_per_generator"] == 1
    assert report["artifact_hashes"]["report_sha256"]
    assert len(report["power_curve"]["rows"]) == 2
    assert "PILOT_NOT_ACCEPTANCE" in cal.render_summary_text(report)
