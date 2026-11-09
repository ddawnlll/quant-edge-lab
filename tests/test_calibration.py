"""TASK-26 / B6 calibration tests (F27 / F28 audit cases).

The heavy B6 workload is not executed here.  These tests pin the generator law,
the HMAC seed protocol parity, the frozen selection rule, the *actual* cohort
Holm family (never padded to the 16-variant screening family), the frozen cohort
alpha-spending schedule, the compute-budget gate, the mandatory generator/effect
coverage guard, checkpoint/retry identity, the Clopper-Pearson bound, the
aggregation/certification rules, and one bounded end-to-end pilot that runs
through :func:`qel.stats.core.family_decision`.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from qel.research import seed as seed_protocol
from qel.stats import calibration as cal
from qel.stats.core import (
    _DECISION_METHOD,
    family_decision,
    holm_adjusted,
    required_resamples,
    two_half_diagnostic,
)

AUDIT_B = 9999
AUDIT_MAX = 10_000
ALPHA_J0 = 0.05 / 2.0


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


def _audit_program(generator: str = "GAUSSIAN_AR1_RHO_0_3", *, cohorts: int = 1, **kwargs) -> dict:
    """Program sized to satisfy the frozen two-half diagnostic (min_half=50)."""
    options = {
        "program": 0,
        "dev_days": 160,
        "fresh_days": 140,
        "cohorts": cohorts,
        "control_replications": 2,
    }
    options.update(kwargs)
    return cal.generate_program(generator, **options)


def _forced_selection(program: dict, selected, *, max_candidates: int = 2) -> dict:
    """Explicit frozen family used to pin zero/one/two-candidate behaviour."""
    return {
        "cohort": 1,
        "development_start": 0,
        "development_end": program["windows"][1][0],
        "development_days": program["windows"][1][0],
        "variants_screened": cal.B6_FAMILY_VARIANTS,
        "scores": [0.0] * cal.B6_FAMILY_VARIANTS,
        "eligible": list(selected),
        "eligible_count": len(selected),
        "selected": list(selected),
        "selection_status": (
            cal.SELECTION_SELECTED if selected else cal.SELECTION_INSUFFICIENT_ELIGIBLE
        ),
        "reason": None if selected else cal.SELECTION_INSUFFICIENT_ELIGIBLE,
        "max_candidates": max_candidates,
    }


# ---------------------------------------------------------------------------
# Generator law, seed protocol, selection
# ---------------------------------------------------------------------------


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


def test_selection_scores_min_and_never_exceeds_cap():
    program = _small_program()
    selection = cal.select_candidates(program, cohort=1)
    assert selection["variants_screened"] == cal.B6_FAMILY_VARIANTS
    assert len(selection["selected"]) <= cal.B6_CANDIDATES_MAX_PER_COHORT
    assert selection["selected"] == selection["eligible"][:2]
    assert selection["eligible_count"] == len(selection["eligible"])
    scores = selection["scores"]
    # Every selected variant has a strictly positive min(T_profit, T_control).
    for variant in selection["selected"]:
        assert scores[variant] > 0.0


def test_empty_eligible_cohort_selects_zero_candidates():
    program = _small_program()
    # Push every candidate far below its own control: no variant can have a
    # positive min(T_profit, T_control) score, so no variant is eligible.
    program["variants"] = program["variants"] - 0.01
    selection = cal.select_candidates(program, cohort=1)
    assert selection["eligible"] == []
    assert selection["eligible_count"] == 0
    assert selection["selected"] == []
    assert selection["selection_status"] == cal.SELECTION_INSUFFICIENT_ELIGIBLE
    assert selection["reason"] == cal.SELECTION_INSUFFICIENT_ELIGIBLE


def test_zero_eligible_cohort_promotes_nothing_and_records_status(monkeypatch):
    empty = _forced_selection(_small_program(), [])
    monkeypatch.setattr(cal, "select_candidates", lambda *args, **kwargs: dict(empty))
    record = cal.run_program(
        "GAUSSIAN_AR1_RHO_0_3",
        program=0,
        cohorts=2,
        dev_days=120,
        fresh_days=140,
        control_replications=2,
    )
    assert record["promotions"] == []
    assert record["selection_counts"] == [0, 0]
    assert record["ineligible_cohorts"] == 2
    for cohort in record["cohorts"]:
        assert cohort["family_status"] == cal.SELECTION_INSUFFICIENT_ELIGIBLE
        assert cohort["entries"] == []
        assert cohort["alpha_spending"]["family_size"] == 0
        assert cohort["alpha_spending"]["b_required"] == 0
        assert cohort["alpha_spending"]["resolved"] is True
    report = cal._aggregate_generator(cal._spec("GAUSSIAN_AR1_RHO_0_3"), [record])
    assert report["promotions"] == 0
    assert report["coverage"]["status"] == "NO_EVALUABLE_ENDPOINTS"
    assert report["certified_stationary"] is False
    assert report["insufficient_eligible_cohorts"] == 2
    assert report["selection"]["family_size_histogram"] == {"0": 2}


def test_cohort_development_set_expands_after_release():
    program = _small_program()
    first = cal.select_candidates(program, cohort=1)
    second = cal.select_candidates(program, cohort=2)
    assert first["development_days"] == 120
    assert second["development_days"] == 180
    assert second["development_end"] > first["development_end"]


# ---------------------------------------------------------------------------
# Actual-cohort Holm family and alpha spending
# ---------------------------------------------------------------------------


def test_holm_step_down_thresholds_and_stop():
    # Holm step-down: m*alpha rank thresholds, and an early failure stops the
    # remaining rejections.  This is the table used by family_decision.
    both = holm_adjusted([0.001, 0.02], 0.05)
    assert both["thresholds"] == pytest.approx([0.025, 0.05])
    assert both["rejected"] == [True, True]
    stopped = holm_adjusted([0.02, 0.06], 0.05)
    assert stopped["thresholds"] == pytest.approx([0.025, 0.05])
    assert stopped["rejected"] == [True, False]


def test_one_eligible_uses_family_size_one():
    program = _audit_program()
    selection = _forced_selection(program, [1])
    assert selection["max_candidates"] == 2
    result = cal.decide_cohort(
        program, cohort=1, selection=selection, b=AUDIT_B, max_resamples=AUDIT_MAX
    )
    assert len(result["entries"]) == 1
    assert result["alpha_spending"]["family_size"] == 1
    assert result["alpha_spending"]["b_required"] == required_resamples(1, 0)
    assert result["alpha_spending"]["alpha_frozen"] == ALPHA_J0
    entry = result["entries"][0]
    decision = entry["decision"]
    assert decision["budget"]["family_size"] == 1
    # m = 1: the single member is tested at alpha_j / 1.
    assert decision["holm_threshold"] == pytest.approx(ALPHA_J0)
    assert decision["adjusted_p"] == pytest.approx(decision["iut_p_upper"])


def test_two_eligible_family_retains_invalid_member_at_p_one():
    program = _audit_program()
    # Variant 0 is flat: the frozen decision path cannot compute a HAC SE, so it
    # is retained in the family at p = 1 instead of being dropped.
    program["variants"][0] = 0.0
    selection = _forced_selection(program, [0, 1])
    result = cal.decide_cohort(
        program, cohort=1, selection=selection, b=AUDIT_B, max_resamples=AUDIT_MAX
    )
    assert result["alpha_spending"]["family_size"] == 2
    assert result["alpha_spending"]["b_required"] == required_resamples(2, 0)
    by_variant = {entry["variant"]: entry["decision"] for entry in result["entries"]}
    assert set(by_variant) == {0, 1}
    invalid = by_variant[0]
    assert invalid["p_iut_upper"] == 1.0
    assert invalid["adjusted_p"] == 1.0
    assert invalid["retained_in_family"] is True
    assert "DEGENERATE_VARIANCE" in invalid["reason_codes"]
    # Step-down over the actual two-member family: the smallest p-value is
    # tested at alpha/2, the retained p=1 member only at alpha/1.
    assert by_variant[1]["holm_threshold"] == pytest.approx(ALPHA_J0 / 2)
    assert invalid["holm_threshold"] == pytest.approx(ALPHA_J0)
    assert result["promoted_variants"] == []


def test_zero_one_two_candidate_alpha_spending_and_b_recorded():
    program = _audit_program(cohorts=3)
    expected_alpha = {1: 0.025, 2: 0.0125, 3: 0.00625}
    families = {1: [], 2: [1], 3: [1, 2]}
    records = []
    for cohort in (1, 2, 3):
        selection = _forced_selection(program, families[cohort])
        result = cal.decide_cohort(
            program,
            cohort=cohort,
            selection=selection,
            b=500,
            max_resamples=AUDIT_MAX,
        )
        alpha = result["alpha_spending"]
        assert alpha["j"] == cohort - 1
        assert alpha["alpha_frozen"] == pytest.approx(expected_alpha[cohort])
        assert alpha["family_size"] == len(families[cohort])
        assert alpha["b_required"] == (
            required_resamples(len(families[cohort]), cohort - 1)
            if families[cohort]
            else 0
        )
        assert alpha["max_resamples"] == AUDIT_MAX
        assert alpha["b_used"] == (500 if families[cohort] else 0)
        # b=500 is below the frozen minimum, so a tested family is unresolved.
        assert alpha["resolved"] is (not families[cohort])
        records.append(result)

    summary = cal._alpha_spending_summary([r["alpha_spending"] for r in records])
    assert [row["j"] for row in summary] == [0, 1, 2]
    assert summary[0]["family_sizes"] == {"0": 1}
    assert summary[1]["family_sizes"] == {"1": 1}
    assert summary[2]["family_sizes"] == {"2": 1}
    assert summary[2]["b_required_by_family_size"] == {"2": required_resamples(2, 2)}


def test_decide_cohort_routes_through_joint_family_decision():
    program = _audit_program()
    selection = _forced_selection(program, [0, 1])
    result = cal.decide_cohort(
        program, cohort=1, selection=selection, b=AUDIT_B, max_resamples=AUDIT_MAX
    )
    # Re-running the accepted core family path on the same panels must give the
    # same verdict and the same joint p-values: no private re-implementation.
    candidates = {
        cal._candidate_label(variant): cal._panel(
            program["variants"][variant], 160, 300, "PORTFOLIO"
        )
        for variant in (0, 1)
    }
    direct = family_decision(
        candidates,
        cal._panel(program["control"], 160, 300, "CONTROL"),
        "1",
        "0",
        "0",
        alpha=0.025,
        block=cal.B6_BLOCK,
        block_sensitivity=cal.B6_BLOCK_SENSITIVITY,
        hac_lag=cal.B6_HAC_LAG,
        b=AUDIT_B,
        max_resamples=AUDIT_MAX,
        seed=cal._family_seed(program, 1, 0),
        family_index=0,
        power_target=0.8,
        batch=cal.B6_BATCH_RESAMPLES,
        min_half=50,
    )
    assert direct["family_size"] == 2
    for entry in result["entries"]:
        cid = entry["candidate_id"]
        core_entry = direct["candidates"][cid]
        core_p = core_entry.get("p_iut_upper", core_entry.get("iut_p_upper"))
        assert entry["decision"]["verdict"] == core_entry["verdict"]
        assert entry["decision"]["iut_p_upper"] == pytest.approx(core_p)
        assert entry["decision"]["adjusted_p"] == pytest.approx(core_entry["adjusted_p"])
        assert core_entry["method_hash"]
        assert entry["decision"]["method"] == _DECISION_METHOD


def test_mc_cap_returns_waiting_compute_budget_not_promotion():
    program = _audit_program()
    selection = _forced_selection(program, [0, 1])
    required = required_resamples(2, 0)
    result = cal.decide_cohort(
        program,
        cohort=1,
        selection=selection,
        b=AUDIT_B,
        max_resamples=required,  # required + 1 does not fit
    )
    assert result["alpha_spending"]["resolved"] is False
    assert result["alpha_spending"]["family_size"] == 2
    for entry in result["entries"]:
        assert entry["decision"]["verdict"] == "WAITING_COMPUTE_BUDGET"
        assert entry["decision"]["error"] == "WAITING_COMPUTE_BUDGET"
    coverage = [cal._endpoint_coverage(entry) for entry in result["entries"]]
    assert [row["status"] for row in coverage] == ["WAITING_COMPUTE_BUDGET"] * 2
    assert result["promoted_variants"] == []


def test_regime_shift_cohort_is_blocked_not_promoted():
    program = _small_program(
        "FROZEN_REGIME_SHIFT_NEGATIVE_CONTROL", cohorts=1, controls=2, fresh_days=140
    )
    selection = _forced_selection(program, [0, 1])
    result = cal.decide_cohort(
        program, cohort=1, selection=selection, b=AUDIT_B, max_resamples=AUDIT_MAX
    )
    for entry in result["entries"]:
        assert entry["decision"]["verdict"] != "PROMOTE"
        assert entry["decision"]["status"] == "BLOCKED"
        assert "NONSTATIONARY" in entry["decision"]["reason_codes"]
    assert result["promoted_variants"] == []


# ---------------------------------------------------------------------------
# Coverage guard, certification and identity
# ---------------------------------------------------------------------------


def _guard_report(name: str, status: str, *, stationary: bool, certified: bool = False) -> dict:
    return {
        "generator": name,
        "design": {"stationary_design": stationary},
        "coverage": {"status": status, "report_present": True},
        "certified_stationary": certified,
    }


def _full_power_rows(*, generators=None, effects=None) -> list[dict]:
    generators = cal.REQUIRED_STATIONARY_GENERATORS if generators is None else generators
    effects = cal.B6_EFFECTS if effects is None else effects
    return [
        {"generator": name, "effect_daily": float(effect), "programs": 1}
        for name in generators
        for effect in effects
    ]


def test_coverage_guard_blocks_missing_generators_and_effects():
    reports = [_guard_report("GAUSSIAN_AR1_RHO_0_3", "PASS", stationary=True)]
    guard = cal.coverage_guard(
        reports=reports,
        power_rows=_full_power_rows(generators=["GAUSSIAN_AR1_RHO_0_3"], effects=[0.0]),
        generators=["GAUSSIAN_AR1_RHO_0_3"],
        effects=[0.0],
    )
    codes = [problem["code"] for problem in guard["problems"]]
    assert guard["complete"] is False
    assert "NULL_GENERATOR_COVERAGE_MISSING" in codes
    assert "EFFECT_GRID_INCOMPLETE" in codes
    assert "POWER_CELL_COVERAGE_MISSING" in codes
    assert "NONSTATIONARY_CONTROL_MISSING" in codes


def test_coverage_guard_complete_matrix():
    reports = [
        _guard_report(name, "PASS", stationary=True)
        for name in cal.REQUIRED_STATIONARY_GENERATORS
    ]
    reports.append(
        _guard_report(
            "FROZEN_REGIME_SHIFT_NEGATIVE_CONTROL",
            "FAILED_OR_BLOCKED_NONSTATIONARY",
            stationary=False,
        )
    )
    guard = cal.coverage_guard(
        reports=reports,
        power_rows=_full_power_rows(),
        generators=list(cal.REQUIRED_GENERATORS),
        effects=list(cal.B6_EFFECTS),
    )
    assert guard["complete"] is True
    assert guard["problems"] == []
    assert guard["power_cells_required"] == len(cal.REQUIRED_STATIONARY_GENERATORS) * 4


def test_coverage_guard_rejects_certified_nonstationary_control():
    reports = [
        _guard_report(name, "PASS", stationary=True)
        for name in cal.REQUIRED_STATIONARY_GENERATORS
    ]
    reports.append(
        _guard_report(
            "FROZEN_REGIME_SHIFT_NEGATIVE_CONTROL", "PASS", stationary=False, certified=True
        )
    )
    guard = cal.coverage_guard(
        reports=reports,
        power_rows=_full_power_rows(),
        generators=list(cal.REQUIRED_GENERATORS),
        effects=list(cal.B6_EFFECTS),
    )
    assert guard["complete"] is False
    assert "NONSTATIONARY_CONTROL_NOT_BLOCKED" in [
        problem["code"] for problem in guard["problems"]
    ]


def test_program_identity_changes_with_labels():
    base = cal.program_identity(generator="GAUSSIAN_AR1_RHO_0_3", program=0)
    same = cal.program_identity(generator="GAUSSIAN_AR1_RHO_0_3", program=0)
    other_program = cal.program_identity(generator="GAUSSIAN_AR1_RHO_0_3", program=1)
    other_generator = cal.program_identity(generator="STUDENT_T_DF5_AR1", program=0)
    other_seed = cal.program_identity(
        generator="GAUSSIAN_AR1_RHO_0_3", program=0, base_seed=1
    )
    assert base == same
    assert len({base, other_program, other_generator, other_seed}) == 4


def _pilot_kwargs(**overrides) -> dict:
    options = {
        "programs": 2,
        "generators": ["GAUSSIAN_AR1_RHO_0_3"],
        "cohorts": 1,
        "dev_days": 80,
        "fresh_days": 60,
        "control_replications": 2,
        "effects": [0.0],
        "power_programs": 1,
        "power_cohorts": 1,
        "family_size": 1,
        "workers": 1,
        "b": 2000,
        "max_resamples": AUDIT_MAX,
    }
    options.update(overrides)
    return options


def test_checkpoint_retry_preserves_identity(tmp_path):
    path = tmp_path / "checkpoint.json"
    first = cal.run_calibration(checkpoint_path=str(path), **_pilot_kwargs())
    assert first["identity"]["checkpoint"]["reused"] == 0
    assert first["identity"]["checkpoint"]["computed"] == 2

    # Second run with the identical labels: every program is reused and the
    # decision digest is unchanged.
    second = cal.run_calibration(checkpoint_path=str(path), **_pilot_kwargs())
    assert second["identity"]["checkpoint"]["reused"] == 2
    assert second["identity"]["checkpoint"]["computed"] == 0
    assert second["identity"]["records_signature"] == first["identity"]["records_signature"]

    # A different seed label invalidates the identity, so nothing is reused and
    # the decision digest changes.
    changed = cal.run_calibration(
        checkpoint_path=str(path), **_pilot_kwargs(base_seed=7)
    )
    assert changed["identity"]["checkpoint"]["reused"] == 0
    assert changed["identity"]["records_signature"] != first["identity"]["records_signature"]
    assert json.loads(path.read_text())["format"] == cal.CHECKPOINT_FORMAT


def test_checkpoint_rejects_foreign_format(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"format": "OTHER", "records": {}}))
    with pytest.raises(ValueError):
        cal.load_checkpoint(path)
    # A missing checkpoint file is an empty checkpoint, not an error.
    assert cal.load_checkpoint(tmp_path / "missing.json")["records"] == {}


# ---------------------------------------------------------------------------
# Clopper-Pearson, aggregation and the labelled pilot
# ---------------------------------------------------------------------------


def test_clopper_pearson_upper_bound():
    assert cal.clopper_pearson_upper(0, 1000) == pytest.approx(0.0029912495, abs=1e-9)
    assert cal.clopper_pearson_upper(0, 1) == pytest.approx(0.95, abs=1e-12)
    assert cal.clopper_pearson_upper(10, 10) == 1.0
    assert cal.clopper_pearson_upper(5, 100) < 0.11
    with pytest.raises(ValueError):
        cal.clopper_pearson_upper(11, 10)


def _record(
    *,
    coverage_status: str,
    verdict: str,
    promotion: bool,
    family_size: int = 2,
    resolved: bool = True,
) -> dict:
    """Compact program record shaped exactly like run_program's output."""
    if coverage_status == "EVALUATED":
        coverage_row = {
            "variant": 0,
            "status": "EVALUATED",
            "covered": True,
            "true_mu": 0.0,
            "ci_low": -0.001,
            "ci_high": 0.001,
            "difference_covered": True,
            "mde_daily": 0.0009,
            "verdict": verdict,
        }
    else:
        coverage_row = {
            "variant": 0,
            "status": coverage_status,
            "covered": None,
            "reason": "NONSTATIONARY",
            "verdict": verdict,
        }
    return {
        "generator": "X",
        "generator_id": 1,
        "program": 0,
        "identity": "i" * 64,
        "promotions": [0] if promotion else [],
        "promotion_count": 1 if promotion else 0,
        "coverage": [coverage_row],
        "verdict_counts": {verdict: 1},
        "blocked_cohorts": 1 if coverage_status == "BLOCKED" else 0,
        "waiting_cohorts": 1 if coverage_status == "WAITING_COMPUTE_BUDGET" else 0,
        "ineligible_cohorts": 0,
        "selection_counts": [family_size],
        "selection_statuses": [cal.SELECTION_SELECTED],
        "selection_incomplete": False,
        "family_variants": cal.B6_FAMILY_VARIANTS,
        "cohort_alpha": [
            {
                "cohort": 1,
                "j": 0,
                "alpha_nominal": ALPHA_J0,
                "alpha_frozen": ALPHA_J0,
                "schedule": cal._ALPHA_SCHEDULE,
                "family_size": family_size,
                "declared_screening_variants": cal.B6_FAMILY_VARIANTS,
                "b_required": required_resamples(family_size, 0),
                "b_used": required_resamples(family_size, 0),
                "max_resamples": cal.B6_MAX_RESAMPLES,
                "resolved": resolved,
                "unresolvable": False,
                "family_status": "PASS",
                "family_verdict": verdict,
                "selection_status": cal.SELECTION_SELECTED,
                "seed": 1,
                "fresh_start": 100,
                "fresh_end": 200,
                "fresh_days": 100,
            }
        ],
        "method_hashes": ["m" * 64],
        "signature": "s" * 64,
        "generation_seconds": 0.1,
        "cpu_seconds": 0.2,
        "elapsed_seconds": 0.2,
        "peak_rss_bytes": 1024,
        "total_days": 300,
        "controls_replications": 4,
    }


def test_aggregate_blocks_insufficient_monte_carlo():
    spec = cal._spec("GAUSSIAN_AR1_RHO_0_3")
    records = [
        _record(coverage_status="EVALUATED", verdict="INSUFFICIENT_EVIDENCE", promotion=False)
        for _ in range(2)
    ]
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
    records = [
        _record(coverage_status="EVALUATED", verdict="NO_PROMOTION", promotion=False)
        for _ in range(1000)
    ]
    report = cal._aggregate_generator(spec, records)
    assert report["coverage"]["status"] == "PASS"
    assert report["clopper_pearson_upper_95"] <= cal.B6_THRESHOLD
    assert report["target_pass"] is True
    assert report["certified_stationary"] is True
    # The actual family size (not the 16-variant screening family) is recorded.
    assert report["cohort_alpha_spending"][0]["family_sizes"] == {"2": 1000}
    assert report["selection"]["family_size_histogram"] == {"2": 1000}


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
    assert "COVERAGE_MATRIX_INCOMPLETE" in report["blockers"]
    assert report["targets"]["coverage_report_present"]["value"] is True
    assert report["targets"]["power_curve_report_present"]["value"] is True
    assert report["protocol"]["decision_method"] == _DECISION_METHOD
    assert report["protocol"]["seed_protocol"] == "HMAC_SHA256_COUNTER_V1"
    assert report["protocol"]["decision_family"] == "ACTUAL_SELECTED_COHORT_NO_PADDING"
    assert report["workload"]["independent_null_programs"] == 1000
    assert report["executed"]["programs_per_generator"] == 1
    assert report["artifact_hashes"]["report_sha256"]
    assert len(report["power_curve"]["rows"]) == 2
    assert report["coverage_matrix"]["complete"] is False
    assert report["identity"]["records_signature"]
    assert "PILOT_NOT_ACCEPTANCE" in cal.render_summary_text(report)
