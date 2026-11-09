"""B6 statistical null and injection calibration (TASK-26 / issue #14).

This module is the *full* B6 workload, not the provisional local tally that
lives in :mod:`qel.stats.core` (``calibrate_nulls``).  Every program is routed
through the same frozen TASK-14 decision path that is used for certification
(:func:`qel.stats.core.paired_decision`): shared calendar and capital,
shared-index stationary bootstrap, HAC re-estimation inside every replicate,
IUT, Holm step-down over a fixed 16-variant family, and the frozen cohort alpha
spending schedule ``0.05 / 2**(j+1)``.

Generator law (frozen by ``benchmarks[B6]`` in
``specification/spec/architecture.json``)::

    f_t       = rho * f_(t-1) + sqrt(1-rho^2) * z_t,  rho = 0.3,
                f_0 = z_0 (stationary, unit variance)
    e_(v,c,t) = same AR(1) law, independent across (variant, coin)
    z         = standard normal, or standardized Student-t5
                (``t5_inverseCDF(u) * sqrt(3/5)``)
    per coin  = mu_variant + 0.01 * (0.6 * f_t + 0.8 * e_(v,c,t))
    portfolio = equal-weight mean of the coins (one generator uses one coin)

Draws use only ``HMAC_SHA256_COUNTER_V1`` from :mod:`qel.research.seed` with the
six frozen labels ``[GENERATOR_ID, PROGRAM_REPLICATION, VARIANT, COIN, TIME,
CONTROL_REPLICATION]``.  No NumPy PCG64 stream is used for a generator draw; the
inverse-CDF transform is the pinned SciPy backend recorded by
``seed.backend_info()``.  The stationary bootstrap *inside* the accepted
decision path is the frozen TASK-14 decision stream, seeded from an
HMAC-derived integer per program/cohort.

Control construction: the matched control is the average of
``CONTROL_REPLICATIONS`` prespecified replications, each replaying the same
common factor with independent idiosyncratic AR(1) paths.  Replication ``r`` is
labelled ``CONTROL_REPLICATION = r + 1`` so that it can never collide with a
candidate draw (``CONTROL_REPLICATION = 0``).

Nonstationary negative control: ``FROZEN_REGIME_SHIFT_NEGATIVE_CONTROL`` adds a
common-factor mean of ``+1`` in the first half and ``-1`` in the second half of
every evaluation window.  It must be blocked by the frozen two-half
stationarity alarm or must expose failed coverage; it is never certified as a
stationary FWER guarantee.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import resource
import struct
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Mapping, Sequence

import numpy as np

from ..contract.canonical import hash_jcs
from ..research import seed as seed_protocol
from .core import (
    _DECISION_METHOD,
    DecisionError,
    hac_se,
    paired_decision,
    required_resamples,
)

# ---------------------------------------------------------------------------
# Frozen B6 workload
# ---------------------------------------------------------------------------

BENCHMARK_ID = "B6"
TASK_ID = "TASK-26"

B6_NULL_PROGRAMS_PER_GENERATOR = 1000
B6_FAMILY_VARIANTS = 16
B6_COHORTS = 3
B6_CANDIDATES_MAX_PER_COHORT = 2
B6_HISTORY_DAYS_DEVELOPMENT = 1500
B6_FRESH_DAYS_PER_COHORT = 500
B6_EFFECTS: tuple[float, ...] = (0.0, 0.0005, 0.001, 0.002)
B6_GENERATOR_SD = 0.01
B6_BLOCK = 5
B6_BLOCK_SENSITIVITY = 10
B6_HAC_LAG = 5
B6_BATCH_RESAMPLES = 64
B6_MAX_RESAMPLES = 51_199
B6_CONTROL_REPLICATIONS = 1000
B6_THRESHOLD = 0.075
B6_ALPHA = 0.025

RHO = 0.3
LOADING_COMMON = 0.6
LOADING_IDIOSYNCRATIC = 0.8
T5_STANDARDIZATION = math.sqrt(3.0 / 5.0)

#: Reserved label slots so the shared common factor and the control namespace
#: can never collide with a candidate draw.
FACTOR_VARIANT = 0xFFFF
FACTOR_COIN = 0xFFFF

_ALPHA_SCHEDULE = "0.05 / 2^(j+1)"


@dataclass(frozen=True)
class GeneratorSpec:
    """Frozen generator descriptor.

    ``coins`` is the number of coins in the equal-weight portfolio.  The frozen
    law names one single-coin generator; the AR(1) and Student-t5 generators are
    implemented as that single-coin law (no cross-sectional pooling), while the
    common-factor and regime-shift generators use the full five-coin portfolio.
    """

    name: str
    generator_id: int
    coins: int
    innovation: str  # "NORMAL" | "T5"
    regime_shift: bool = False
    stationary: bool = True

    def public(self) -> dict:
        return {
            "generator": self.name,
            "generator_id": self.generator_id,
            "coins": self.coins,
            "innovation": self.innovation,
            "regime_shift": self.regime_shift,
            "stationary_design": self.stationary,
        }


GENERATORS: dict[str, GeneratorSpec] = {
    "GAUSSIAN_AR1_RHO_0_3": GeneratorSpec(
        name="GAUSSIAN_AR1_RHO_0_3",
        generator_id=1,
        coins=1,
        innovation="NORMAL",
    ),
    "STUDENT_T_DF5_AR1": GeneratorSpec(
        name="STUDENT_T_DF5_AR1",
        generator_id=2,
        coins=1,
        innovation="T5",
    ),
    "FIVE_COIN_COMMON_FACTOR_LOADING_0_6": GeneratorSpec(
        name="FIVE_COIN_COMMON_FACTOR_LOADING_0_6",
        generator_id=3,
        coins=5,
        innovation="NORMAL",
    ),
    "FROZEN_REGIME_SHIFT_NEGATIVE_CONTROL": GeneratorSpec(
        name="FROZEN_REGIME_SHIFT_NEGATIVE_CONTROL",
        generator_id=4,
        coins=5,
        innovation="NORMAL",
        regime_shift=True,
        stationary=False,
    ),
}

#: Frozen interpretation of the B6 generator-law prose, recorded in every
#: report so an auditor can see exactly what was executed.
LAW_INTERPRETATION = {
    "single_coin_generators": ["GAUSSIAN_AR1_RHO_0_3", "STUDENT_T_DF5_AR1"],
    "five_coin_generators": [
        "FIVE_COIN_COMMON_FACTOR_LOADING_0_6",
        "FROZEN_REGIME_SHIFT_NEGATIVE_CONTROL",
    ],
    "candidate_control_replication_label": 0,
    "control_replication_labels": "1..CONTROL_REPLICATIONS (never 0)",
    "factor_labels": {"VARIANT": FACTOR_VARIANT, "COIN": FACTOR_COIN},
    "regime_shift": "common factor mean +1 first half, -1 second half of every evaluation window",
}


def _spec(generator: str) -> GeneratorSpec:
    try:
        return GENERATORS[generator]
    except KeyError as exc:  # pragma: no cover - defensive
        raise ValueError(f"UNKNOWN_GENERATOR {generator}") from exc


# ---------------------------------------------------------------------------
# HMAC_SHA256_COUNTER_V1 draw series (batched label encoding, one HMAC per draw)
# ---------------------------------------------------------------------------

_KEY = seed_protocol._KEY
_MESSAGE_TAG = seed_protocol._MESSAGE_TAG
_COUNTER_TAG = seed_protocol._COUNTER_TAG
_LABELS = seed_protocol.LABELS
_UNIFORM_SCALE = 2.0**-53
_U64 = struct.Struct(">Q")


def _label_chunk(name: str, value: int) -> bytes:
    return name.encode("ascii") + b"\x00" + _U64.pack(int(value))


def _head(generator_id: int, program: int, variant: int, coin: int) -> bytes:
    """Encoded prefix ``GENERATOR_ID|PROGRAM_REPLICATION|VARIANT|COIN``."""
    return b"\x1f".join(
        [
            _label_chunk("GENERATOR_ID", generator_id),
            _label_chunk("PROGRAM_REPLICATION", program),
            _label_chunk("VARIANT", variant),
            _label_chunk("COIN", coin),
        ]
    )


def _tail(control_replication: int) -> bytes:
    """Encoded suffix ``CONTROL_REPLICATION`` plus counter 0."""
    return (
        b"\x1f"
        + _label_chunk("CONTROL_REPLICATION", control_replication)
        + _COUNTER_TAG
        + _U64.pack(0)
    )


_TIME_CHUNKS = tuple(_label_chunk("TIME", t) for t in range(1 << 16))


def _time_chunk(t: int) -> bytes:
    if t < len(_TIME_CHUNKS):
        return _TIME_CHUNKS[t]
    return _label_chunk("TIME", t)


def uniform_series(
    *,
    generator_id: int,
    program: int,
    variant: int,
    coin: int,
    control_replication: int,
    days: int,
) -> np.ndarray:
    """Draw one uniform per ``TIME`` value for one frozen label tuple.

    The seed protocol places ``TIME`` inside the label tuple, so each time step
    is one HMAC block.  The label encoding is precomputed once per series, which
    is the only safe batching (the protocol is untouched: the message bytes and
    the 53-bit extraction are byte-identical to
    :func:`qel.research.seed.uniform_at`).
    """
    head = _MESSAGE_TAG + _head(generator_id, program, variant, coin) + b"\x1f"
    suffix = _tail(control_replication)
    out = np.empty(days, dtype=float)
    new = hmac.new
    key = _KEY
    for t in range(days):
        message = head + _time_chunk(t) + suffix
        word = int.from_bytes(new(key, message, hashlib.sha256).digest()[:8], "big")
        out[t] = (word >> 11) * _UNIFORM_SCALE
    return out


def _standard_normal(u: np.ndarray) -> np.ndarray:
    return seed_protocol._scipy_norm.ppf(np.clip(u, seed_protocol.U_MIN, seed_protocol.U_MAX))


def _standardized_t5(u: np.ndarray) -> np.ndarray:
    values = seed_protocol._scipy_t.ppf(np.clip(u, seed_protocol.U_MIN, seed_protocol.U_MAX), 5)
    return values * T5_STANDARDIZATION


def _innovation(
    spec: GeneratorSpec,
    *,
    program: int,
    variant: int,
    coin: int,
    control_replication: int,
    days: int,
) -> np.ndarray:
    u = uniform_series(
        generator_id=spec.generator_id,
        program=program,
        variant=variant,
        coin=coin,
        control_replication=control_replication,
        days=days,
    )
    if spec.innovation == "T5":
        return _standardized_t5(u)
    return _standard_normal(u)


def ar1(z: np.ndarray, rho: float = RHO) -> np.ndarray:
    """Stationary unit-variance AR(1) recursion with ``x_0 = z_0``."""
    z = np.asarray(z, dtype=float)
    out = np.empty_like(z)
    out[0] = z[0]
    scale = math.sqrt(1.0 - rho * rho)
    for t in range(1, z.size):
        out[t] = rho * out[t - 1] + scale * z[t]
    return out


def _windows(dev_days: int, fresh_days: int, cohorts: int) -> list[tuple[int, int]]:
    """Evaluation windows over the program timeline: dev, cohort 1..n."""
    windows = [(0, dev_days)]
    start = dev_days
    for _ in range(cohorts):
        windows.append((start, start + fresh_days))
        start += fresh_days
    return windows


def _apply_regime_shift(factor: np.ndarray, windows: Sequence[tuple[int, int]]) -> np.ndarray:
    """Add the explicit nonstationary mean shift inside every evaluation window."""
    shifted = factor.copy()
    for start, end in windows:
        middle = start + (end - start) // 2
        shifted[start:middle] += 1.0
        shifted[middle:end] -= 1.0
    return shifted


# ---------------------------------------------------------------------------
# Program generation
# ---------------------------------------------------------------------------


def generate_program(
    generator: str,
    *,
    program: int,
    dev_days: int = B6_HISTORY_DAYS_DEVELOPMENT,
    fresh_days: int = B6_FRESH_DAYS_PER_COHORT,
    cohorts: int = B6_COHORTS,
    control_replications: int = B6_CONTROL_REPLICATIONS,
    effects: Mapping[int, float] | None = None,
) -> dict:
    """Build the full candidate/control panel for one program.

    ``effects`` maps a variant index to an injected daily ``mu``.  Only
    prespecified variants receive a nonzero effect; every other variant has
    ``mu = 0``.
    """
    spec = _spec(generator)
    effects = dict(effects or {})
    windows = _windows(dev_days, fresh_days, cohorts)
    total_days = windows[-1][1]

    factor_z = _innovation(
        spec,
        program=program,
        variant=FACTOR_VARIANT,
        coin=FACTOR_COIN,
        control_replication=0,
        days=total_days,
    )
    factor = ar1(factor_z)
    if spec.regime_shift:
        factor = _apply_regime_shift(factor, windows)

    variants = np.arange(B6_FAMILY_VARIANTS)
    coins = np.arange(spec.coins)
    idio = np.empty((variants.size, coins.size, total_days), dtype=float)
    for v in variants:
        for c in coins:
            idio[v, c, :] = ar1(
                _innovation(
                    spec,
                    program=program,
                    variant=int(v),
                    coin=int(c),
                    control_replication=0,
                    days=total_days,
                )
            )

    # Candidate portfolio = equal-weight mean over coins of the per-coin law.
    common = LOADING_COMMON * factor
    return_variants = np.empty((variants.size, total_days), dtype=float)
    for v in variants:
        mu = float(effects.get(int(v), 0.0))
        per_coin = common[None, :] + LOADING_IDIOSYNCRATIC * idio[v]
        return_variants[v] = mu + B6_GENERATOR_SD * per_coin.mean(axis=0)

    # Control = average of 1000 prespecified replications, each replaying the
    # same common factor with independent idiosyncratic paths.  Accumulated in
    # chunks so peak memory stays bounded.
    control = B6_GENERATOR_SD * LOADING_COMMON * factor
    control_accumulator = np.zeros(total_days, dtype=float)
    chunk = 100
    done = 0
    while done < control_replications:
        size = min(chunk, control_replications - done)
        for r in range(done, done + size):
            rep = np.zeros(total_days, dtype=float)
            for c in coins:
                rep += ar1(
                    _innovation(
                        spec,
                        program=program,
                        variant=int(variants[0]),
                        coin=int(c),
                        control_replication=r + 1,
                        days=total_days,
                    )
                )
            control_accumulator += rep / coins.size
        done += size
    control = control + B6_GENERATOR_SD * LOADING_IDIOSYNCRATIC * (
        control_accumulator / max(control_replications, 1)
    )

    return {
        "generator": spec.name,
        "generator_id": spec.generator_id,
        "program": program,
        "spec": spec,
        "windows": windows,
        "total_days": total_days,
        "factor": factor,
        "variants": return_variants,
        "control": control,
        "effects": {int(k): float(v) for k, v in effects.items()},
        "control_replications": control_replications,
    }


# ---------------------------------------------------------------------------
# Frozen selection
# ---------------------------------------------------------------------------


def _score(series: np.ndarray) -> float:
    try:
        se = hac_se(series, lag=B6_HAC_LAG)
    except ValueError:
        return float("-inf")
    return float(np.mean(series) / se)


def select_candidates(
    program: dict,
    *,
    cohort: int,
    max_candidates: int = B6_CANDIDATES_MAX_PER_COHORT,
    family_variants: int = B6_FAMILY_VARIANTS,
) -> dict:
    """Frozen ``min(T_profit, T_control)`` selection on development days.

    The development set is the initial development window plus every already
    released cohort.  Only the top ``max_candidates`` eligible variants advance;
    earlier released cohorts become development-only after release.
    """
    windows = program["windows"]
    start = 0
    end = windows[cohort][0]  # development only: everything before this cohort
    if end <= start:
        raise ValueError("INSUFFICIENT_DEVELOPMENT")
    candidate = program["variants"][:, start:end]
    control = program["control"][start:end]
    scores = []
    for v in range(min(family_variants, candidate.shape[0])):
        t_profit = _score(candidate[v])
        t_control = _score(candidate[v] - control)
        scores.append(min(t_profit, t_control))
    order = sorted(range(len(scores)), key=lambda i: (-scores[i], i))
    eligible = [i for i in order if scores[i] > 0.0]
    chosen = (eligible or order)[:max_candidates]
    return {
        "cohort": cohort,
        "development_start": start,
        "development_end": end,
        "development_days": end - start,
        "variants_screened": len(scores),
        "scores": [float(s) for s in scores],
        "eligible": [int(i) for i in eligible],
        "selected": [int(i) for i in chosen],
        "max_candidates": int(max_candidates),
    }


# ---------------------------------------------------------------------------
# Decision bridge: the accepted TASK-14 path
# ---------------------------------------------------------------------------


def _panel(series: np.ndarray, start: int, end: int, symbol: str) -> dict:
    """Shared UTC daily calendar; day index is mapped to a fixed ISO date."""
    base = date(2020, 1, 1)
    days = {
        (base + timedelta(days=start + i)).isoformat(): f"{value:.12f}"
        for i, value in enumerate(series[start:end])
    }
    return {symbol: days}


def decide_cohort(
    program: dict,
    *,
    cohort: int,
    selection: Mapping[int, float] | None = None,
    family_size: int = B6_FAMILY_VARIANTS,
    b: int | None = None,
    max_resamples: int | None = None,
    base_seed: int = 0,
) -> dict:
    """Run selection plus the frozen paired decision for one cohort.

    ``cohort`` is 1-based (cohort 1 is the first fresh out-of-sample window) and
    doubles as the frozen alpha-spending family index ``j = cohort - 1``.
    """
    windows = program["windows"]
    start, end = windows[cohort]
    family_index = cohort - 1
    alpha = 0.05 / (2.0 ** (family_index + 1))
    required = required_resamples(family_size, family_index)
    b = required if b is None else int(b)
    # The frozen resolution rule is ``B + 1 <= max_resamples``.
    max_resamples = (required + 1 if max_resamples is None else int(max_resamples))
    selection = selection or select_candidates(program, cohort=cohort)
    selected = list(selection["selected"])
    effects = program["effects"]

    entries = []
    for variant in selected:
        candidate = program["variants"][variant]
        control = program["control"]
        seed_value = int(
            hashlib.sha256(
                f"{program['generator_id']}|{program['program']}|{cohort}|{variant}|{base_seed}".encode()
            ).hexdigest()[:16],
            16,
        ) % (1 << 31)
        try:
            decision = paired_decision(
                _panel(candidate, start, end, "PORTFOLIO"),
                _panel(control, start, end, "CONTROL"),
                "1",
                "0",
                "0",
                alpha=alpha,
                block=B6_BLOCK,
                block_sensitivity=B6_BLOCK_SENSITIVITY,
                hac_lag=B6_HAC_LAG,
                b=b,
                max_resamples=max_resamples,
                seed=seed_value,
                family_size=family_size,
                family_index=family_index,
                candidate_id=f"VARIANT-{variant:02d}",
                power_target=0.8,
                batch=B6_BATCH_RESAMPLES,
                min_half=50,
            )
        except DecisionError as exc:
            decision = {
                "status": "BLOCKED",
                "verdict": "INSUFFICIENT_EVIDENCE",
                "error": exc.code,
                "reason_codes": [exc.code],
                "endpoints": None,
                "primary": None,
            }
        entries.append(
            {
                "variant": int(variant),
                "true_mu": float(effects.get(int(variant), 0.0)),
                "decision": decision,
            }
        )

    verdicts = [entry["decision"].get("verdict") for entry in entries]
    promoted = [entry["variant"] for entry in entries if entry["decision"].get("verdict") == "PROMOTE"]
    return {
        "cohort": cohort,
        "family_index": family_index,
        "alpha_spending": {
            "cohort": cohort,
            "j": family_index,
            "alpha_nominal": alpha,
            "alpha_frozen": 0.05 / (2.0 ** (family_index + 1)),
            "schedule": _ALPHA_SCHEDULE,
            "family_size": family_size,
            "b_required": required,
            "b_used": min(b, max_resamples),
            "max_resamples": max_resamples,
            "resolved": min(b, max_resamples) >= required,
            "fresh_start": start,
            "fresh_end": end,
            "fresh_days": end - start,
        },
        "selection": selection,
        "entries": entries,
        "verdicts": verdicts,
        "promoted_variants": promoted,
    }


def _endpoint_coverage(entry: Mapping) -> dict:
    decision = entry["decision"]
    if decision.get("primary") is None:
        blocked = decision.get("reason_codes") or [decision.get("error") or "BLOCKED"]
        return {
            "variant": entry["variant"],
            "status": "BLOCKED",
            "covered": None,
            "reason": blocked[0],
            "true_mu": entry["true_mu"],
        }
    endpoint_a = decision["primary"]["endpoints"]["A"]
    endpoint_b = decision["primary"]["endpoints"]["B"]
    low, high = endpoint_a["ci_low_two_sided"], endpoint_a["ci_high_two_sided"]
    low_b, high_b = endpoint_b["ci_low_two_sided"], endpoint_b["ci_high_two_sided"]
    return {
        "variant": entry["variant"],
        "status": "EVALUATED",
        "true_mu": entry["true_mu"],
        "covered": bool(low <= entry["true_mu"] <= high),
        "ci_low": low,
        "ci_high": high,
        "difference_covered": bool(low_b <= 0.0 <= high_b),
        "ci_low_difference": low_b,
        "ci_high_difference": high_b,
        "point_estimate": endpoint_a["mean_daily_return"],
        "mde_daily": max(endpoint_a["mde_daily"], endpoint_b["mde_daily"]),
        "verdict": decision.get("verdict"),
    }


# ---------------------------------------------------------------------------
# Statistics helpers
# ---------------------------------------------------------------------------


def clopper_pearson_upper(k: int, n: int, confidence: float = 0.95) -> float:
    """Exact one-sided Clopper-Pearson upper bound for a binomial proportion."""
    if n <= 0 or k < 0 or k > n:
        raise ValueError("INVALID_BINOMIAL_INPUT")
    if k >= n:
        return 1.0
    from scipy.stats import beta as _beta

    return float(_beta.ppf(confidence, k + 1, n - k))


def _peak_rss_bytes() -> int:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(usage) * 1024  # Linux reports KiB


# ---------------------------------------------------------------------------
# One program end to end (worker unit)
# ---------------------------------------------------------------------------


def run_program(
    generator: str,
    *,
    program: int,
    cohorts: int = B6_COHORTS,
    dev_days: int = B6_HISTORY_DAYS_DEVELOPMENT,
    fresh_days: int = B6_FRESH_DAYS_PER_COHORT,
    control_replications: int = B6_CONTROL_REPLICATIONS,
    effects: Mapping[int, float] | None = None,
    family_size: int = B6_FAMILY_VARIANTS,
    max_candidates: int = B6_CANDIDATES_MAX_PER_COHORT,
    b: int | None = None,
    max_resamples: int | None = None,
    base_seed: int = 0,
) -> dict:
    """Generate one program and run the full frozen selection/decision sequence."""
    started = time.perf_counter()
    generated = generate_program(
        generator,
        program=program,
        dev_days=dev_days,
        fresh_days=fresh_days,
        cohorts=cohorts,
        control_replications=control_replications,
        effects=effects,
    )
    generation_seconds = time.perf_counter() - started
    programs = []
    promoted = []
    coverage = []
    verdict_counts: dict[str, int] = {}
    blocked_cohorts = 0
    for cohort in range(1, cohorts + 1):
        selection = select_candidates(
            generated,
            cohort=cohort,
            max_candidates=max_candidates,
            family_variants=family_size,
        )
        result = decide_cohort(
            generated,
            cohort=cohort,
            selection=selection,
            family_size=family_size,
            b=b,
            max_resamples=max_resamples,
            base_seed=base_seed,
        )
        for entry in result["entries"]:
            coverage.append(_endpoint_coverage(entry))
            verdict = entry["decision"].get("verdict")
            verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1
        if any(entry["decision"].get("primary") is None for entry in result["entries"]):
            blocked_cohorts += 1
        promoted.extend(result["promoted_variants"])
        programs.append(result)

    return {
        "generator": generator,
        "program": program,
        "cohorts": programs,
        "promotions": sorted(set(promoted)),
        "promotion_count": len(set(promoted)),
        "coverage": coverage,
        "verdict_counts": verdict_counts,
        "blocked_cohorts": blocked_cohorts,
        "generation_seconds": generation_seconds,
        "elapsed_seconds": time.perf_counter() - started,
        "peak_rss_bytes": _peak_rss_bytes(),
        "total_days": generated["total_days"],
        "controls_replications": control_replications,
        "selection_counts": [len(p["selection"]["selected"]) for p in programs],
    }


# ---------------------------------------------------------------------------
# Aggregation and certification
# ---------------------------------------------------------------------------


def _aggregate_generator(spec: GeneratorSpec, records: Sequence[Mapping]) -> dict:
    total = len(records)
    promotions = sum(1 for record in records if record["promotion_count"] > 0)
    rate = promotions / total if total else 0.0
    upper = clopper_pearson_upper(promotions, total) if total else 1.0
    coverage_rows = [row for record in records for row in record["coverage"]]
    evaluated = [row for row in coverage_rows if row["status"] == "EVALUATED"]
    blocked = [row for row in coverage_rows if row["status"] == "BLOCKED"]
    waiting = [row for row in coverage_rows if row["status"] == "WAITING_COMPUTE_BUDGET"]
    covered = [row for row in evaluated if row["covered"]]
    coverage_rate = (len(covered) / len(evaluated)) if evaluated else None
    reasons: dict[str, int] = {}
    for row in blocked:
        reasons[row["reason"]] = reasons.get(row["reason"], 0) + 1
    if waiting:
        coverage_status = "WAITING_COMPUTE_BUDGET"
    elif spec.stationary and blocked:
        coverage_status = "BLOCKED"
    elif spec.stationary and coverage_rate is not None:
        coverage_status = "PASS" if coverage_rate >= 0.90 else "FAILED_COVERAGE"
    elif not spec.stationary:
        # The negative control must block or expose failed coverage.
        coverage_status = "FAILED_OR_BLOCKED_NONSTATIONARY" if (blocked or coverage_rate is None or coverage_rate < 0.90) else "PASS"
    else:  # pragma: no cover - defensive
        coverage_status = "BLOCKED"
    verdict_counts: dict[str, int] = {}
    for record in records:
        for verdict, count in record["verdict_counts"].items():
            verdict_counts[verdict] = verdict_counts.get(verdict, 0) + count
    alpha_records = [
        cohort["alpha_spending"] for record in records for cohort in record["cohorts"]
    ]
    method_hashes = sorted(
        {
            entry["decision"].get("method_hash")
            for record in records
            for cohort in record["cohorts"]
            for entry in cohort["entries"]
            if entry["decision"].get("method_hash")
        }
    )
    elapsed = sum(record["elapsed_seconds"] for record in records)
    peak = max((record["peak_rss_bytes"] for record in records), default=0)
    target_upper = upper if spec.stationary else None
    return {
        "generator": spec.name,
        "design": spec.public(),
        "programs": total,
        "promotions": promotions,
        "false_promotion_rate": rate,
        "clopper_pearson_upper_95": upper,
        "threshold": B6_THRESHOLD,
        "target_applicable": spec.stationary,
        "target_pass": (upper <= B6_THRESHOLD) if spec.stationary else None,
        "certified_stationary": bool(spec.stationary and (upper <= B6_THRESHOLD) and coverage_status in ("PASS",)),
        "coverage": {
            "status": coverage_status,
            "evaluated_endpoints": len(evaluated),
            "covered_endpoints": len(covered),
            "coverage_rate": coverage_rate,
            "blocked_endpoints": len(blocked),
            "waiting_endpoints": len(waiting),
            "blocked_reasons": reasons,
        },
        "verdict_counts": verdict_counts,
        "blocked_cohorts": sum(record["blocked_cohorts"] for record in records),
        "selection": {
            "variants_screened": B6_FAMILY_VARIANTS,
            "candidates_max_per_cohort": B6_CANDIDATES_MAX_PER_COHORT,
            "selected_total": sum(sum(record["selection_counts"]) for record in records),
            "programs_with_selection": sum(
                1 for record in records if all(count > 0 for count in record["selection_counts"])
            ),
            "incomplete": any(
                count < B6_CANDIDATES_MAX_PER_COHORT
                for record in records
                for count in record["selection_counts"]
            ),
        },
        "cohort_alpha_spending": alpha_records,
        "method_hashes": method_hashes,
        "resources": {
            "elapsed_seconds": elapsed,
            "max_program_peak_rss_bytes": peak,
            "max_program_elapsed_seconds": max(
                (record["elapsed_seconds"] for record in records), default=0.0
            ),
        },
    }


def _power_curve(
    *,
    generator: str,
    effects: Sequence[float],
    programs: int,
    cohorts: int,
    dev_days: int,
    fresh_days: int,
    control_replications: int,
    family_size: int,
    base_seed: int,
    workers: int,
) -> dict:
    """Power/MDE curve for the prescribed effect grid on one generator.

    Each effect level runs ``programs`` independent programs with the effect
    injected on the prespecified variant ``VARIANT-00``; every other variant
    keeps ``mu = 0``.  A program counts as a detection when the injected variant
    is promoted in any cohort.
    """
    tasks = [
        (generator, index, effect, cohorts, dev_days, fresh_days, control_replications, family_size, base_seed)
        for effect in effects
        for index in range(programs)
    ]
    results = _map(_power_worker, tasks, workers)
    rows = []
    for effect in effects:
        matching = [r for r in results if r["effect"] == effect]
        detected = sum(1 for r in matching if r["promoted"])
        mde_values = [
            row["mde_daily"] for r in matching for row in r["coverage"] if row.get("status") == "EVALUATED"
        ]
        rows.append(
            {
                "effect_daily": float(effect),
                "programs": len(matching),
                "detections": detected,
                "power": (detected / len(matching)) if matching else None,
                "mde_daily_median": float(np.median(mde_values)) if mde_values else None,
                "cohorts": cohorts,
            }
        )
    return {
        "generator": generator,
        "injected_variant": 0,
        "effects": [float(e) for e in effects],
        "rows": rows,
        "programs_per_effect": programs,
    }


def _power_worker(args) -> dict:
    generator, index, effect, cohorts, dev_days, fresh_days, control_replications, family_size, base_seed = args
    effects = {0: float(effect)}
    record = run_program(
        generator,
        program=index,
        cohorts=cohorts,
        dev_days=dev_days,
        fresh_days=fresh_days,
        control_replications=control_replications,
        effects=effects,
        family_size=family_size,
        base_seed=base_seed,
    )
    injected = [row for row in record["coverage"] if row["variant"] == 0]
    return {
        "effect": float(effect),
        "program": index,
        "promoted": 0 in record["promotions"],
        "coverage": injected,
        "verdict_counts": record["verdict_counts"],
    }


def _run_worker(args) -> dict:
    generator, program, cohorts, dev_days, fresh_days, control_replications, family_size, base_seed = args
    return run_program(
        generator,
        program=program,
        cohorts=cohorts,
        dev_days=dev_days,
        fresh_days=fresh_days,
        control_replications=control_replications,
        family_size=family_size,
        base_seed=base_seed,
        effects=None,
    )


def _map(function, tasks, workers: int) -> list:
    workers = max(1, min(int(workers), 6, max(len(tasks), 1)))
    if workers == 1:
        return [function(task) for task in tasks]
    from concurrent.futures import ProcessPoolExecutor

    with ProcessPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(function, tasks))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def b6_workload() -> dict:
    """The frozen B6 workload descriptor (no local reductions applied)."""
    return {
        "benchmark": BENCHMARK_ID,
        "task": TASK_ID,
        "independent_null_programs": B6_NULL_PROGRAMS_PER_GENERATOR,
        "family_variants": B6_FAMILY_VARIANTS,
        "cohorts": B6_COHORTS,
        "candidates_max_per_cohort": B6_CANDIDATES_MAX_PER_COHORT,
        "history_days_development": B6_HISTORY_DAYS_DEVELOPMENT,
        "fresh_days_per_cohort": B6_FRESH_DAYS_PER_COHORT,
        "null_generators": list(GENERATORS),
        "injected_daily_effects": list(B6_EFFECTS),
        "generator_sd": B6_GENERATOR_SD,
        "selection": "FULL_FROZEN_WALKFORWARD_SELECTION_PLUS_IUT_PLUS_HOLM_PLUS_ALPHA_SPENDING",
        "block_length_primary": B6_BLOCK,
        "block_length_sensitivity": B6_BLOCK_SENSITIVITY,
        "hac_lag": B6_HAC_LAG,
        "batch_resamples_max": B6_BATCH_RESAMPLES,
        "control_replications": B6_CONTROL_REPLICATIONS,
        "random_protocol": "HMAC_SHA256_COUNTER_V1;inverseCDFnormal/t5inpinnedSciPy",
        "nonstationary_expected": "BLOCK_OR_REPORT_FAILED_COVERAGE_NOT_CERTIFY",
        "generator_law": {
            "rho": RHO,
            "loading_common": LOADING_COMMON,
            "loading_idiosyncratic": LOADING_IDIOSYNCRATIC,
            "standardized_t5": "t5_inverseCDF(U)*sqrt(3/5)",
            "seed_labels": list(_LABELS),
        },
        "law_interpretation": LAW_INTERPRETATION,
    }


def run_calibration(
    *,
    programs: int = B6_NULL_PROGRAMS_PER_GENERATOR,
    generators: Sequence[str] | None = None,
    cohorts: int = B6_COHORTS,
    dev_days: int = B6_HISTORY_DAYS_DEVELOPMENT,
    fresh_days: int = B6_FRESH_DAYS_PER_COHORT,
    control_replications: int = B6_CONTROL_REPLICATIONS,
    effects: Sequence[float] | None = None,
    power_programs: int = 2,
    power_cohorts: int | None = None,
    family_size: int = B6_FAMILY_VARIANTS,
    workers: int = 4,
    base_seed: int = 0,
    max_resamples: int | None = None,
    b: int | None = None,
    label: str | None = None,
) -> dict:
    """Run the B6 program: per-generator null calibration plus injected power.

    A run is ``ACCEPTANCE`` only when every stationary generator meets the 95%
    Clopper-Pearson upper bound target, the coverage and power-curve reports are
    present, the full frozen workload was executed (1000 programs, 1500/500
    days, 3 cohorts, 16 variants, 1000 control replications), the Monte Carlo
    resolution was met, and the nonstationary control was blocked.  Anything
    less is ``PILOT_NOT_ACCEPTANCE`` with explicit blockers.
    """
    started = time.perf_counter()
    generators = list(generators or GENERATORS)
    effects = list(B6_EFFECTS if effects is None else effects)
    for name in generators:
        _spec(name)
    if programs < 1:
        raise ValueError("programs must be >= 1")
    if control_replications < 1:
        raise ValueError("control_replications must be >= 1")

    full_programs = programs >= B6_NULL_PROGRAMS_PER_GENERATOR
    full_days = (
        dev_days == B6_HISTORY_DAYS_DEVELOPMENT
        and fresh_days == B6_FRESH_DAYS_PER_COHORT
    )
    full_controls = control_replications == B6_CONTROL_REPLICATIONS
    full_cohorts = cohorts == B6_COHORTS
    full_family = family_size == B6_FAMILY_VARIANTS

    null_tasks = [
        (
            name,
            index,
            cohorts,
            dev_days,
            fresh_days,
            control_replications,
            family_size,
            base_seed,
        )
        for name in generators
        for index in range(programs)
    ]
    # One heavy workload at a time: null calibration first, then the power grid.
    records = _map(_run_worker, null_tasks, workers)
    by_generator = {name: [r for r in records if r["generator"] == name] for name in generators}
    reports = [_aggregate_generator(_spec(name), by_generator[name]) for name in generators]

    power_cohorts = cohorts if power_cohorts is None else power_cohorts
    power_report = _power_curve(
        generator=generators[0],
        effects=effects,
        programs=power_programs,
        cohorts=power_cohorts,
        dev_days=dev_days,
        fresh_days=fresh_days,
        control_replications=control_replications,
        family_size=family_size,
        base_seed=base_seed,
        workers=workers,
    )

    stationary_reports = [r for r in reports if r["design"]["stationary_design"]]
    nonstationary_reports = [r for r in reports if not r["design"]["stationary_design"]]
    max_upper = max((r["clopper_pearson_upper_95"] for r in stationary_reports), default=1.0)
    fwer_target = {
        "metric": "null_fwer_95cp_upper_per_generator",
        "operator": "LE",
        "threshold": B6_THRESHOLD,
        "value": max_upper,
        "per_generator": {
            r["generator"]: r["clopper_pearson_upper_95"] for r in stationary_reports
        },
        "pass": bool(stationary_reports) and max_upper <= B6_THRESHOLD,
    }
    coverage_present = bool(reports) and all(r["coverage"]["status"] for r in reports)
    power_present = bool(power_report["rows"])
    nonstationary_ok = True
    for report in nonstationary_reports:
        blocked = report["coverage"]["status"] in (
            "FAILED_OR_BLOCKED_NONSTATIONARY",
            "BLOCKED",
            "FAILED_COVERAGE",
        )
        nonstationary_ok = nonstationary_ok and blocked and not report["certified_stationary"]

    resolution_met = all(
        all(alpha["resolved"] for alpha in r["cohort_alpha_spending"]) for r in reports
    ) and bool(reports)
    incomplete_selection = any(r["selection"]["incomplete"] for r in reports)
    full_workload = (
        full_programs
        and full_days
        and full_controls
        and full_cohorts
        and full_family
        and power_programs >= B6_NULL_PROGRAMS_PER_GENERATOR
    )

    blockers: list[str] = []
    if not full_workload:
        blockers.append("COMPUTE_BUDGET_INSUFFICIENT")
    if not fwer_target["pass"]:
        blockers.append("NULL_FWER_UPPER_ABOVE_TARGET")
    if not coverage_present:
        blockers.append("COVERAGE_REPORT_MISSING")
    if not power_present:
        blockers.append("POWER_CURVE_REPORT_MISSING")
    if not nonstationary_ok:
        blockers.append("NONSTATIONARY_CONTROL_NOT_BLOCKED")
    if not resolution_met:
        blockers.append("MC_UNDER_RESOLVED")
    if incomplete_selection:
        blockers.append("INCOMPLETE_PROGRAM_SELECTION")

    acceptance = (
        full_workload
        and not blockers
        and fwer_target["pass"]
        and coverage_present
        and power_present
        and nonstationary_ok
        and resolution_met
        and not incomplete_selection
    )
    elapsed = time.perf_counter() - started
    peak_rss = _peak_rss_bytes()

    estimated_program_seconds = max(
        (r["resources"]["max_program_elapsed_seconds"] for r in reports), default=0.0
    )
    core_seconds = (
        B6_NULL_PROGRAMS_PER_GENERATOR
        * len(GENERATORS)
        * estimated_program_seconds
        + B6_NULL_PROGRAMS_PER_GENERATOR
        * len(B6_EFFECTS)
        * estimated_program_seconds
    )

    payload = {
        "status": "ACCEPTANCE" if acceptance else "PILOT_NOT_ACCEPTANCE",
        "benchmark": BENCHMARK_ID,
        "task": TASK_ID,
        "label": label or ("B6_ACCEPTANCE" if acceptance else "PILOT_NOT_ACCEPTANCE"),
        "workload": b6_workload(),
        "executed": {
            "generators": generators,
            "programs_per_generator": programs,
            "cohorts": cohorts,
            "history_days_development": dev_days,
            "fresh_days_per_cohort": fresh_days,
            "control_replications": control_replications,
            "family_variants": family_size,
            "power_programs_per_effect": power_programs,
            "power_cohorts": power_cohorts,
            "base_seed": base_seed,
            "workers": workers,
        },
        "protocol": {
            "seed_protocol": seed_protocol.PROTOCOL,
            "seed_protocol_hash": seed_protocol.PROTOCOL_HASH,
            "backend": seed_protocol.backend_info(),
            "decision_method": _DECISION_METHOD,
            "alpha_schedule": _ALPHA_SCHEDULE,
            "family_size": family_size,
            "required_resamples_per_cohort": {
                f"j{j}": required_resamples(family_size, j) for j in range(max(cohorts, 1))
            },
        },
        "generators": reports,
        "power_curve": power_report,
        "targets": {
            "null_fwer_95cp_upper_per_generator": fwer_target,
            "coverage_report_present": {"value": coverage_present, "operator": "EQ", "expected": True, "pass": coverage_present},
            "power_curve_report_present": {"value": power_present, "operator": "EQ", "expected": True, "pass": power_present},
        },
        "nonstationary_control": {
            "generators": [r["generator"] for r in nonstationary_reports],
            "blocked_or_failed_coverage": nonstationary_ok,
            "certified_stationary": any(r["certified_stationary"] for r in nonstationary_reports),
        },
        "resolution": {
            "monte_carlo_resolved": resolution_met,
            "program_selection_complete": not incomplete_selection,
        },
        "compute_budget": {
            "pilot_wall_seconds": elapsed,
            "programs_completed": sum(r["programs"] for r in reports),
            "estimated_seconds_per_program": estimated_program_seconds,
            "estimated_full_program_core_seconds": core_seconds,
            "estimated_full_program_core_hours": core_seconds / 3600.0,
            "full_workload_required": {
                "programs_per_generator": B6_NULL_PROGRAMS_PER_GENERATOR,
                "generators": len(GENERATORS),
                "cohorts": B6_COHORTS,
                "history_days_development": B6_HISTORY_DAYS_DEVELOPMENT,
                "fresh_days_per_cohort": B6_FRESH_DAYS_PER_COHORT,
                "control_replications": B6_CONTROL_REPLICATIONS,
                "effects": list(B6_EFFECTS),
            },
        },
        "blockers": blockers,
        "resources": {
            "wall_seconds": elapsed,
            "peak_rss_bytes": peak_rss,
            "workers": workers,
        },
    }
    payload["artifact_hashes"] = {
        "report_sha256": _sha256_json(payload),
        "workload_sha256": hash_jcs(b6_workload()),
        "method_hashes": sorted(
            {h for r in reports for h in r["method_hashes"]}
        ),
        "seed_protocol_hash": seed_protocol.PROTOCOL_HASH,
        "backend_hash": seed_protocol.BACKEND_HASH,
    }
    return payload


def _sha256_json(payload: Mapping) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def render_summary_text(report: Mapping) -> str:
    """Human-readable evidence summary (mirrors the JSON artifact)."""
    lines = [
        f"{report['benchmark']} / {report['task']} - {report['status']}",
        f"label: {report['label']}",
        f"seed protocol hash: {report['protocol']['seed_protocol_hash']}",
        f"decision method: {report['protocol']['decision_method']}",
        "",
        "per generator:",
    ]
    for row in report["generators"]:
        coverage = row["coverage"]
        lines.append(
            f"  {row['generator']}: programs={row['programs']} promotions={row['promotions']} "
            f"rate={row['false_promotion_rate']:.6f} cp95_upper={row['clopper_pearson_upper_95']:.6f} "
            f"target_pass={row['target_pass']} coverage={coverage['status']} "
            f"coverage_rate={coverage['coverage_rate']} "
            f"blocked_endpoints={coverage['blocked_endpoints']} "
            f"certified_stationary={row['certified_stationary']}"
        )
    lines.append("")
    lines.append("power curve (injected variant 0):")
    for row in report["power_curve"]["rows"]:
        lines.append(
            f"  effect={row['effect_daily']} programs={row['programs']} "
            f"detections={row['detections']} power={row['power']} "
            f"mde_daily_median={row['mde_daily_median']}"
        )
    lines.append("")
    lines.append(f"targets: {json.dumps(report['targets'], sort_keys=True)}")
    lines.append(f"blockers: {report['blockers']}")
    resources = report["resources"]
    lines.append(
        f"resources: wall={resources['wall_seconds']:.3f}s "
        f"peak_rss={resources['peak_rss_bytes']} B workers={resources['workers']}"
    )
    lines.append(
        f"compute budget: est_s_per_program={report['compute_budget']['estimated_seconds_per_program']:.3f} "
        f"est_full_core_hours={report['compute_budget']['estimated_full_program_core_hours']:.2f}"
    )
    lines.append(f"artifact report_sha256: {report['artifact_hashes']['report_sha256']}")
    return "\n".join(lines) + "\n"
