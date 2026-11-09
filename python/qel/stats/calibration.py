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
import os
import resource
import struct
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np

from ..contract.canonical import hash_jcs
from ..research import seed as seed_protocol
from .core import (
    _DECISION_METHOD,
    DecisionError,
    family_decision,
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

#: The mandatory B6 generator/effect coverage matrix.  Certification requires
#: every entry to be represented; a missing cell is a blocker, never a pass.
REQUIRED_GENERATORS: tuple[str, ...] = (
    "GAUSSIAN_AR1_RHO_0_3",
    "STUDENT_T_DF5_AR1",
    "FIVE_COIN_COMMON_FACTOR_LOADING_0_6",
    "FROZEN_REGIME_SHIFT_NEGATIVE_CONTROL",
)
REQUIRED_EFFECTS: tuple[float, ...] = B6_EFFECTS

CHECKPOINT_FORMAT = "QEL-B6-CHECKPOINT-V1"

#: A zero-eligible cohort performs no test and can promotion nothing.  It is a
#: recorded outcome (``INSUFFICIENT_ELIGIBLE``), not a silently shrunk family.
SELECTION_SELECTED = "SELECTED"
SELECTION_INSUFFICIENT_ELIGIBLE = "INSUFFICIENT_ELIGIBLE"


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

#: Power cells are required for the stationary laws only.  The regime-shift
#: negative control is required to *block*, not to show power: a promotion on a
#: nonstationary control is a failure of the alarm, not evidence of an edge.
REQUIRED_STATIONARY_GENERATORS: tuple[str, ...] = tuple(
    name for name, spec in GENERATORS.items() if spec.stationary
)

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

    Eligibility is a hard gate: a variant with a non-positive frozen score never
    advances.  When no variant is eligible the cohort selects zero candidates,
    records ``INSUFFICIENT_ELIGIBLE`` and can promote nothing.  There is no
    "best effort" fallback that would silently test an ineligible variant.
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
    limit = max(0, int(max_candidates))
    chosen = eligible[:limit]
    status = SELECTION_SELECTED if chosen else SELECTION_INSUFFICIENT_ELIGIBLE
    return {
        "cohort": cohort,
        "development_start": start,
        "development_end": end,
        "development_days": end - start,
        "variants_screened": len(scores),
        "scores": [float(s) for s in scores],
        "eligible": [int(i) for i in eligible],
        "eligible_count": len(eligible),
        "selected": [int(i) for i in chosen],
        "selection_status": status,
        "reason": None if chosen else SELECTION_INSUFFICIENT_ELIGIBLE,
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


def _family_seed(program: Mapping, cohort: int, base_seed: int) -> int:
    """Frozen decision-stream seed for one program/cohort family.

    Every candidate in a cohort shares one stationary-bootstrap index stream,
    as the joint decision path requires.  Variant identity enters through the
    candidate label and the frozen generator draws (``VARIANT`` label), never by
    handing each candidate a private decision stream.
    """
    payload = f"{program['generator_id']}|{program['program']}|{cohort}|{int(base_seed)}"
    return int(hashlib.sha256(payload.encode()).hexdigest()[:16], 16) % (1 << 31)


def _candidate_label(variant: int) -> str:
    return f"VARIANT-{int(variant):02d}"


def decide_cohort(
    program: dict,
    *,
    cohort: int,
    selection: Mapping | None = None,
    family_size: int = B6_FAMILY_VARIANTS,
    b: int | None = None,
    max_resamples: int | None = None,
    base_seed: int = 0,
) -> dict:
    """Run selection plus the frozen *joint family* decision for one cohort.

    ``cohort`` is 1-based (cohort 1 is the first fresh out-of-sample window) and
    doubles as the frozen alpha-spending family index ``j = cohort - 1``.

    The Holm family is the actual selected cohort (zero, one or two candidates).
    It is never padded to the 16-variant screening family.  An invalid or
    blocked frozen member is retained at ``p = 1`` by
    :func:`qel.stats.core.family_decision`, so a cohort can never gain power by
    silently shrinking its family.
    """
    windows = program["windows"]
    start, end = windows[cohort]
    family_index = cohort - 1
    alpha = 0.05 / (2.0 ** (family_index + 1))
    selection = (
        selection if selection is not None else select_candidates(program, cohort=cohort)
    )
    selected = [int(v) for v in selection.get("selected", [])]
    if len(selected) > int(selection.get("max_candidates", B6_CANDIDATES_MAX_PER_COHORT)):
        raise ValueError("SELECTION_EXCEEDS_MAX_CANDIDATES")
    if len(set(selected)) != len(selected):
        raise ValueError("SELECTION_DUPLICATE_CANDIDATE")
    if any(v < 0 or v >= int(family_size) for v in selected):
        raise ValueError("SELECTION_OUTSIDE_FAMILY")
    actual_family = len(selected)
    max_resamples = B6_MAX_RESAMPLES if max_resamples is None else int(max_resamples)
    if max_resamples < 1:
        raise ValueError("max_resamples must be >= 1")
    seed_value = _family_seed(program, cohort, base_seed)
    effects = program["effects"]
    selection_status = selection.get(
        "selection_status",
        SELECTION_SELECTED if selected else SELECTION_INSUFFICIENT_ELIGIBLE,
    )

    if actual_family == 0:
        # No eligible variant: no test exists, nothing can be promoted, and the
        # zero-sized family is recorded rather than padded.
        return {
            "cohort": cohort,
            "family_index": family_index,
            "seed": seed_value,
            "family_status": SELECTION_INSUFFICIENT_ELIGIBLE,
            "family_verdict": SELECTION_INSUFFICIENT_ELIGIBLE,
            "alpha_spending": {
                "cohort": cohort,
                "j": family_index,
                "alpha_nominal": alpha,
                "alpha_frozen": alpha,
                "schedule": _ALPHA_SCHEDULE,
                "family_size": 0,
                "declared_screening_variants": int(family_size),
                "b_required": 0,
                "b_used": 0,
                "max_resamples": max_resamples,
                "resolved": True,
                "unresolvable": False,
                "family_status": SELECTION_INSUFFICIENT_ELIGIBLE,
                "family_verdict": SELECTION_INSUFFICIENT_ELIGIBLE,
                "selection_status": selection_status,
                "seed": seed_value,
                "fresh_start": start,
                "fresh_end": end,
                "fresh_days": end - start,
            },
            "selection": selection,
            "entries": [],
            "verdicts": [],
            "promoted_variants": [],
            "method_hashes": [],
        }

    required = required_resamples(actual_family, family_index)
    b_value = required if b is None else int(b)
    candidates = {
        _candidate_label(variant): _panel(
            program["variants"][variant], start, end, "PORTFOLIO"
        )
        for variant in selected
    }
    try:
        family = family_decision(
            candidates,
            _panel(program["control"], start, end, "CONTROL"),
            "1",
            "0",
            "0",
            alpha=alpha,
            block=B6_BLOCK,
            block_sensitivity=B6_BLOCK_SENSITIVITY,
            hac_lag=B6_HAC_LAG,
            b=b_value,
            max_resamples=max_resamples,
            seed=seed_value,
            family_index=family_index,
            power_target=0.8,
            batch=B6_BATCH_RESAMPLES,
            min_half=50,
        )
    except DecisionError as exc:
        # Structural failure of the family itself: keep every member in the
        # frozen family at p = 1 and report a block.
        family = {
            "status": "BLOCKED",
            "verdict": "INSUFFICIENT_EVIDENCE",
            "error": exc.code,
            "family_size": actual_family,
            "family_index": family_index,
            "budget": {
                "b_requested": b_value,
                "b_used": min(b_value, max_resamples),
                "b_required": required,
                "max_resamples": max_resamples,
                "resolved": min(b_value, max_resamples) >= required,
            },
            "candidates": {
                _candidate_label(variant): {
                    "status": "BLOCKED",
                    "verdict": "INSUFFICIENT_EVIDENCE",
                    "error": exc.code,
                    "reason_codes": [exc.code, "HOLM_RETAINED_WITH_P1"],
                    "retained_in_family": True,
                    "p_iut_upper": 1.0,
                    "adjusted_p": 1.0,
                    "primary": None,
                }
                for variant in selected
            },
        }

    entries = []
    for variant in selected:
        decision = dict(family["candidates"][_candidate_label(variant)])
        # family_decision exposes the per-member IUT p-value as ``p_iut_upper``;
        # mirror it under the decision-path name so downstream aggregation and
        # the frozen decision signature see one field on every member.
        if "iut_p_upper" not in decision:
            decision["iut_p_upper"] = float(decision.get("p_iut_upper", 1.0))
        if decision.get("verdict") == "WAITING_COMPUTE_BUDGET":
            decision.setdefault("error", "WAITING_COMPUTE_BUDGET")
        entries.append(
            {
                "variant": variant,
                "candidate_id": _candidate_label(variant),
                "true_mu": float(effects.get(variant, 0.0)),
                "retained_in_family": bool(decision.get("retained_in_family", True)),
                "decision": decision,
            }
        )
    verdicts = [entry["decision"].get("verdict") for entry in entries]
    promoted = [
        entry["variant"]
        for entry in entries
        if entry["decision"].get("verdict") == "PROMOTE"
    ]
    budget = family.get("budget", {})
    waiting = family.get("verdict") == "WAITING_COMPUTE_BUDGET"
    resolved = bool(budget.get("resolved", False)) and not waiting
    method_hashes = sorted(
        {
            entry["decision"].get("method_hash")
            for entry in entries
            if entry["decision"].get("method_hash")
        }
        | ({family.get("method_hash")} if family.get("method_hash") else set())
    )
    return {
        "cohort": cohort,
        "family_index": family_index,
        "seed": seed_value,
        "family_status": family.get("status"),
        "family_verdict": family.get("verdict"),
        "alpha_spending": {
            "cohort": cohort,
            "j": family_index,
            "alpha_nominal": alpha,
            "alpha_frozen": alpha,
            "schedule": _ALPHA_SCHEDULE,
            "family_size": actual_family,
            "declared_screening_variants": int(family_size),
            "b_required": required,
            "b_used": int(budget.get("b_used", min(b_value, max_resamples))),
            "max_resamples": max_resamples,
            "resolved": resolved,
            "unresolvable": bool(budget.get("unresolvable", required + 1 > max_resamples)),
            "family_status": family.get("status"),
            "family_verdict": family.get("verdict"),
            "selection_status": selection_status,
            "seed": seed_value,
            "fresh_start": start,
            "fresh_end": end,
            "fresh_days": end - start,
        },
        "selection": selection,
        "entries": entries,
        "verdicts": verdicts,
        "promoted_variants": promoted,
        "method_hashes": method_hashes,
    }


def _endpoint_coverage(entry: Mapping) -> dict:
    decision = entry["decision"]
    if decision.get("primary") is None:
        blocked = decision.get("reason_codes") or [decision.get("error") or "BLOCKED"]
        waiting = (
            decision.get("verdict") == "WAITING_COMPUTE_BUDGET"
            or decision.get("error") == "WAITING_COMPUTE_BUDGET"
        )
        return {
            "variant": entry["variant"],
            "status": "WAITING_COMPUTE_BUDGET" if waiting else "BLOCKED",
            "covered": None,
            "reason": blocked[0],
            "true_mu": entry["true_mu"],
            "verdict": decision.get("verdict"),
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


def _cpu_seconds() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return float(usage.ru_utime + usage.ru_stime)


def program_identity(
    *,
    generator: str,
    program: int,
    cohorts: int = B6_COHORTS,
    dev_days: int = B6_HISTORY_DAYS_DEVELOPMENT,
    fresh_days: int = B6_FRESH_DAYS_PER_COHORT,
    control_replications: int = B6_CONTROL_REPLICATIONS,
    family_variants: int = B6_FAMILY_VARIANTS,
    max_candidates: int = B6_CANDIDATES_MAX_PER_COHORT,
    base_seed: int = 0,
) -> str:
    """Stable identity of one generator/program under the frozen protocol.

    Two runs with the same identity must reproduce the same draws and the same
    decisions.  A checkpoint entry is reused only when its recorded identity
    matches this value exactly, so changing any frozen label forces a recompute
    instead of silently mixing results.
    """
    spec = _spec(generator)
    return hash_jcs(
        {
            "format": CHECKPOINT_FORMAT,
            "generator": spec.name,
            "generator_id": spec.generator_id,
            "coins": spec.coins,
            "innovation": spec.innovation,
            "regime_shift": spec.regime_shift,
            "program": int(program),
            "cohorts": int(cohorts),
            "dev_days": int(dev_days),
            "fresh_days": int(fresh_days),
            "control_replications": int(control_replications),
            "family_variants": int(family_variants),
            "max_candidates": int(max_candidates),
            "base_seed": int(base_seed),
            "block": B6_BLOCK,
            "block_sensitivity": B6_BLOCK_SENSITIVITY,
            "hac_lag": B6_HAC_LAG,
            "decision_method": _DECISION_METHOD,
            "seed_protocol_hash": seed_protocol.PROTOCOL_HASH,
        }
    )


def decision_signature(record: Mapping) -> str:
    """Digest of the decisions in one program record (no timing, no memory)."""
    cohorts = []
    for cohort in record.get("cohorts", []):
        entries = cohort.get("entries", [])
        cohorts.append(
            {
                "cohort": cohort.get("cohort"),
                "family_index": cohort.get("family_index"),
                "seed": cohort.get("seed"),
                "family_size": cohort.get("alpha_spending", {}).get("family_size"),
                "family_verdict": cohort.get("family_verdict"),
                "selection_status": cohort.get("selection", {}).get("selection_status"),
                "selected": list(cohort.get("selection", {}).get("selected", [])),
                "members": [
                    {
                        "candidate_id": entry.get("candidate_id"),
                        "verdict": entry.get("decision", {}).get("verdict"),
                        "reason_codes": list(entry.get("decision", {}).get("reason_codes") or []),
                        "adjusted_p": entry.get("decision", {}).get("adjusted_p"),
                        "iut_p_upper": entry.get("decision", {}).get("iut_p_upper"),
                    }
                    for entry in entries
                ],
            }
        )
    return hash_jcs(
        {
            "format": CHECKPOINT_FORMAT,
            "generator_id": record.get("generator_id"),
            "program": record.get("program"),
            "promotions": list(record.get("promotions", [])),
            "cohorts": cohorts,
        }
    )


#: Fields a checkpoint retains.  Full per-candidate decision payloads are
#: dropped so a 1000-program checkpoint stays bounded; the aggregation and the
#: decision signature only need these.
CHECKPOINT_FIELDS: tuple[str, ...] = (
    "generator",
    "generator_id",
    "program",
    "identity",
    "promotions",
    "promotion_count",
    "coverage",
    "verdict_counts",
    "blocked_cohorts",
    "waiting_cohorts",
    "ineligible_cohorts",
    "selection_counts",
    "selection_statuses",
    "selection_incomplete",
    "family_variants",
    "cohort_alpha",
    "method_hashes",
    "signature",
    "generation_seconds",
    "cpu_seconds",
    "elapsed_seconds",
    "peak_rss_bytes",
    "total_days",
    "controls_replications",
)


def compact_record(record: Mapping) -> dict:
    """Checkpoint/aggregation view of a program record."""
    return {field: record[field] for field in CHECKPOINT_FIELDS if field in record}


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
    cpu_started = _cpu_seconds()
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
    waiting_cohorts = 0
    ineligible_cohorts = 0
    method_hashes: set[str] = set()
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
        if result["alpha_spending"]["selection_status"] == SELECTION_INSUFFICIENT_ELIGIBLE:
            ineligible_cohorts += 1
        if result.get("family_verdict") == "WAITING_COMPUTE_BUDGET":
            waiting_cohorts += 1
        elif any(
            entry["decision"].get("primary") is None for entry in result["entries"]
        ):
            blocked_cohorts += 1
        method_hashes.update(result.get("method_hashes") or [])
        promoted.extend(result["promoted_variants"])
        programs.append(result)

    record = {
        "generator": generator,
        "generator_id": _spec(generator).generator_id,
        "program": program,
        "identity": program_identity(
            generator=generator,
            program=program,
            cohorts=cohorts,
            dev_days=dev_days,
            fresh_days=fresh_days,
            control_replications=control_replications,
            family_variants=family_size,
            max_candidates=max_candidates,
            base_seed=base_seed,
        ),
        "cohorts": programs,
        "promotions": sorted(set(promoted)),
        "promotion_count": len(set(promoted)),
        "coverage": coverage,
        "verdict_counts": verdict_counts,
        "blocked_cohorts": blocked_cohorts,
        "waiting_cohorts": waiting_cohorts,
        "ineligible_cohorts": ineligible_cohorts,
        "selection_counts": [len(p["selection"]["selected"]) for p in programs],
        "selection_statuses": [
            p["selection"]["selection_status"] for p in programs
        ],
        "selection_incomplete": any(
            p["selection"]["selection_status"]
            not in (SELECTION_SELECTED, SELECTION_INSUFFICIENT_ELIGIBLE)
            for p in programs
        ),
        "family_variants": int(family_size),
        "cohort_alpha": [p["alpha_spending"] for p in programs],
        "method_hashes": sorted(method_hashes),
        "generation_seconds": generation_seconds,
        "cpu_seconds": _cpu_seconds() - cpu_started,
        "elapsed_seconds": time.perf_counter() - started,
        "peak_rss_bytes": _peak_rss_bytes(),
        "total_days": generated["total_days"],
        "controls_replications": control_replications,
    }
    record["signature"] = decision_signature(record)
    return record


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
    elif spec.stationary:
        coverage_status = "NO_EVALUABLE_ENDPOINTS"
    elif not spec.stationary:
        # The negative control can never be certified stationary.  It must
        # block or expose failed coverage; a clean-looking nonstationary run is
        # reported as uncertified, not as a pass.
        coverage_status = (
            "FAILED_OR_BLOCKED_NONSTATIONARY"
            if (blocked or coverage_rate is None or coverage_rate < 0.90)
            else "NONSTATIONARY_UNCERTIFIED"
        )
    else:  # pragma: no cover - defensive
        coverage_status = "BLOCKED"
    verdict_counts: dict[str, int] = {}
    for record in records:
        for verdict, count in record["verdict_counts"].items():
            verdict_counts[verdict] = verdict_counts.get(verdict, 0) + count
    alpha_records = [alpha for record in records for alpha in record.get("cohort_alpha", [])]
    method_hashes = sorted(
        {digest for record in records for digest in record.get("method_hashes", [])}
    )
    elapsed = sum(record["elapsed_seconds"] for record in records)
    cpu = sum(record.get("cpu_seconds", 0.0) for record in records)
    peak = max((record["peak_rss_bytes"] for record in records), default=0)
    family_sizes: dict[str, int] = {}
    for record in records:
        for count in record.get("selection_counts", []):
            family_sizes[str(int(count))] = family_sizes.get(str(int(count)), 0) + 1
    zero_eligible = sum(record.get("ineligible_cohorts", 0) for record in records)
    return {
        "generator": spec.name,
        "design": spec.public(),
        "programs": total,
        "promotions": promotions,
        "programs_with_promotion": promotions,
        "false_promotion_rate": rate,
        "clopper_pearson_upper_95": upper,
        "threshold": B6_THRESHOLD,
        "target_applicable": spec.stationary,
        "target_pass": (upper <= B6_THRESHOLD) if spec.stationary else None,
        "certified_stationary": bool(spec.stationary and (upper <= B6_THRESHOLD) and coverage_status in ("PASS",)),
        "coverage": {
            "status": coverage_status,
            "report_present": bool(coverage_rows) or total > 0,
            "evaluated_endpoints": len(evaluated),
            "covered_endpoints": len(covered),
            "coverage_rate": coverage_rate,
            "blocked_endpoints": len(blocked),
            "waiting_endpoints": len(waiting),
            "blocked_reasons": reasons,
        },
        "verdict_counts": verdict_counts,
        "blocked_cohorts": sum(record["blocked_cohorts"] for record in records),
        "waiting_cohorts": sum(record.get("waiting_cohorts", 0) for record in records),
        "insufficient_eligible_cohorts": zero_eligible,
        "selection": {
            "variants_screened": max(
                (record.get("family_variants", B6_FAMILY_VARIANTS) for record in records),
                default=B6_FAMILY_VARIANTS,
            ),
            "candidates_max_per_cohort": B6_CANDIDATES_MAX_PER_COHORT,
            "selected_total": sum(sum(record["selection_counts"]) for record in records),
            "programs_with_selection": sum(
                1 for record in records if all(count > 0 for count in record["selection_counts"])
            ),
            "family_size_histogram": family_sizes,
            # Zero-eligible cohorts are a recorded outcome (no test, no
            # promotion), not a silently shrunk family.  ``incomplete`` is
            # reserved for a selection that could not run at all.
            "incomplete": any(
                bool(record.get("selection_incomplete", False)) for record in records
            ),
            "zero_eligible_policy": "NO_CANDIDATE_NO_TEST",
        },
        "cohort_alpha_spending": _alpha_spending_summary(alpha_records),
        "cohort_alpha_spending_records": len(alpha_records),
        "method_hashes": method_hashes,
        "resources": {
            "elapsed_seconds": elapsed,
            "cpu_seconds": cpu,
            "max_program_peak_rss_bytes": peak,
            "max_program_elapsed_seconds": max(
                (record["elapsed_seconds"] for record in records), default=0.0
            ),
            "median_program_elapsed_seconds": (
                float(np.median([record["elapsed_seconds"] for record in records]))
                if records
                else 0.0
            ),
        },
    }


def _alpha_spending_summary(alpha_records: Sequence[Mapping]) -> list[dict]:
    """Bounded per-cohort alpha/B table (never one row per program)."""
    rows: dict[tuple[int, int], dict] = {}
    for record in alpha_records:
        key = (int(record["cohort"]), int(record["j"]))
        row = rows.setdefault(
            key,
            {
                "cohort": int(record["cohort"]),
                "j": int(record["j"]),
                "alpha_frozen": float(record["alpha_frozen"]),
                "schedule": record["schedule"],
                "cohorts": 0,
                "family_sizes": {},
                "b_required_by_family_size": {},
                "max_resamples": int(record["max_resamples"]),
                "unresolved": 0,
                "unresolvable": 0,
                "insufficient_eligible": 0,
            },
        )
        row["cohorts"] += 1
        family_size = int(record["family_size"])
        row["family_sizes"][str(family_size)] = (
            row["family_sizes"].get(str(family_size), 0) + 1
        )
        row["b_required_by_family_size"][str(family_size)] = int(
            record["b_required"]
        )
        if not record.get("resolved", False):
            row["unresolved"] += 1
        if record.get("unresolvable", False):
            row["unresolvable"] += 1
        if record.get("family_size", 0) == 0:
            row["insufficient_eligible"] += 1
    return [rows[key] for key in sorted(rows)]


def _power_curve(
    *,
    generators: Sequence[str],
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
    """Power/MDE curve for the prescribed generator x effect grid.

    Every generator law runs every effect level with ``programs`` independent
    programs; the effect is injected on the prespecified variant ``VARIANT-00``
    and every other variant keeps ``mu = 0``.  A program counts as a detection
    when the injected variant is promoted in any cohort.
    """
    tasks = [
        {
            "generator": generator,
            "program": index,
            "effect": float(effect),
            "cohorts": cohorts,
            "dev_days": dev_days,
            "fresh_days": fresh_days,
            "control_replications": control_replications,
            "family_size": family_size,
            "base_seed": base_seed,
        }
        for generator in generators
        for effect in effects
        for index in range(programs)
    ]
    results = _map(_power_worker, tasks, workers)
    rows = []
    for generator in generators:
        for effect in effects:
            matching = [
                r
                for r in results
                if r["generator"] == generator and r["effect"] == float(effect)
            ]
            detected = sum(1 for r in matching if r["promoted"])
            mde_values = [
                row["mde_daily"]
                for r in matching
                for row in r["coverage"]
                if row.get("status") == "EVALUATED"
            ]
            evaluated = [
                row
                for r in matching
                for row in r["coverage"]
                if row.get("status") == "EVALUATED"
            ]
            covered = [row for row in evaluated if row["covered"]]
            rows.append(
                {
                    "generator": generator,
                    "effect_daily": float(effect),
                    "programs": len(matching),
                    "detections": detected,
                    "power": (detected / len(matching)) if matching else None,
                    "mde_daily_median": (
                        float(np.median(mde_values)) if mde_values else None
                    ),
                    "evaluated_injected_endpoints": len(evaluated),
                    "coverage_rate": (
                        len(covered) / len(evaluated) if evaluated else None
                    ),
                    "cohorts": cohorts,
                }
            )
    return {
        "injected_variant": 0,
        "generators": list(generators),
        "effects": [float(e) for e in effects],
        "rows": rows,
        "programs_per_effect": programs,
    }


def _power_worker(task: Mapping) -> dict:
    effect = float(task["effect"])
    record = run_program(
        task["generator"],
        program=int(task["program"]),
        cohorts=int(task["cohorts"]),
        dev_days=int(task["dev_days"]),
        fresh_days=int(task["fresh_days"]),
        control_replications=int(task["control_replications"]),
        effects={0: effect},
        family_size=int(task["family_size"]),
        base_seed=int(task["base_seed"]),
    )
    injected = [row for row in record["coverage"] if row["variant"] == 0]
    return {
        "generator": task["generator"],
        "effect": effect,
        "program": int(task["program"]),
        "promoted": 0 in record["promotions"],
        "coverage": injected,
        "verdict_counts": record["verdict_counts"],
        "elapsed_seconds": record["elapsed_seconds"],
        "signature": record["signature"],
    }


def _run_worker(task: Mapping) -> dict:
    return run_program(
        task["generator"],
        program=int(task["program"]),
        cohorts=int(task["cohorts"]),
        dev_days=int(task["dev_days"]),
        fresh_days=int(task["fresh_days"]),
        control_replications=int(task["control_replications"]),
        family_size=int(task["family_size"]),
        base_seed=int(task["base_seed"]),
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
# Mandatory generator/effect coverage and checkpoint/retry identity
# ---------------------------------------------------------------------------


def coverage_guard(
    *,
    reports: Sequence[Mapping],
    power_rows: Sequence[Mapping],
    generators: Sequence[str],
    effects: Sequence[float],
) -> dict:
    """Certification guard over the mandatory B6 generator/effect matrix.

    Missing coverage blocks certification; it is never treated as a pass.  The
    regime-shift negative control must appear with a blocked/uncertified
    coverage status, and its report may never be certified stationary.
    """
    problems: list[dict] = []
    present = [str(report["generator"]) for report in reports]
    missing_generators = [name for name in REQUIRED_GENERATORS if name not in present]
    if missing_generators:
        problems.append(
            {"code": "NULL_GENERATOR_COVERAGE_MISSING", "missing": missing_generators}
        )
    effects_run = {round(float(effect), 12) for effect in effects}
    missing_effects = [
        float(effect)
        for effect in REQUIRED_EFFECTS
        if round(float(effect), 12) not in effects_run
    ]
    if missing_effects:
        problems.append(
            {"code": "EFFECT_GRID_INCOMPLETE", "missing": missing_effects}
        )
    power_cells = {
        (str(row.get("generator")), round(float(row.get("effect_daily", -1)), 12))
        for row in power_rows
        if int(row.get("programs", 0)) > 0
    }
    required_cells = [
        (name, float(effect))
        for name in REQUIRED_STATIONARY_GENERATORS
        for effect in REQUIRED_EFFECTS
    ]
    missing_cells = [
        f"{name}|{effect}"
        for name, effect in required_cells
        if (name, round(effect, 12)) not in power_cells
    ]
    if missing_cells:
        problems.append(
            {"code": "POWER_CELL_COVERAGE_MISSING", "missing": missing_cells}
        )
    statuses = {
        str(report["generator"]): report["coverage"]["status"] for report in reports
    }
    certified_nonstationary = [
        str(report["generator"])
        for report in reports
        if not report["design"]["stationary_design"]
        and (
            report.get("certified_stationary")
            or report["coverage"]["status"] in ("PASS", "NONSTATIONARY_UNCERTIFIED")
        )
    ]
    if certified_nonstationary:
        problems.append(
            {
                "code": "NONSTATIONARY_CONTROL_NOT_BLOCKED",
                "generators": certified_nonstationary,
            }
        )
    nonstationary_names = [
        name for name in REQUIRED_GENERATORS if not _spec(name).stationary
    ]
    missing_nonstationary = [
        name for name in nonstationary_names if name not in statuses
    ]
    if missing_nonstationary:
        problems.append(
            {
                "code": "NONSTATIONARY_CONTROL_MISSING",
                "missing": missing_nonstationary,
            }
        )
    return {
        "complete": not problems,
        "problems": problems,
        "required_generators": list(REQUIRED_GENERATORS),
        "required_stationary_power_generators": list(REQUIRED_STATIONARY_GENERATORS),
        "required_effects": [float(effect) for effect in REQUIRED_EFFECTS],
        "covered_generators": present,
        "covered_effects": sorted(effects_run),
        "power_cells_present": len(power_cells),
        "power_cells_required": len(required_cells),
        "coverage_status_by_generator": statuses,
    }


def load_checkpoint(path: str | Path) -> dict:
    """Load a B6 checkpoint; a missing file is an empty checkpoint."""
    target = Path(path)
    if not target.exists():
        return {"format": CHECKPOINT_FORMAT, "records": {}}
    try:
        payload = json.loads(target.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError("INVALID_CHECKPOINT") from exc
    if payload.get("format") != CHECKPOINT_FORMAT:
        raise ValueError("INVALID_CHECKPOINT")
    records = payload.get("records")
    if not isinstance(records, dict):
        raise ValueError("INVALID_CHECKPOINT")
    return payload


def write_checkpoint(path: str | Path, records: Sequence[Mapping]) -> dict:
    """Atomically write compact program records keyed by identity."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    entries = {str(record["identity"]): compact_record(record) for record in records}
    payload = {
        "format": CHECKPOINT_FORMAT,
        "records": entries,
        "records_count": len(entries),
    }
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True))
    os.replace(temporary, target)
    return {"path": str(target), "records": len(entries)}


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
    checkpoint_path: str | None = None,
    budget_seconds: float = 2700.0,
) -> dict:
    """Run the B6 program: per-generator null calibration plus injected power.

    A run is ``ACCEPTANCE`` only when every stationary generator meets the 95%
    Clopper-Pearson upper bound target, the coverage and power-curve reports are
    present, the full frozen workload was executed (1000 programs, 1500/500
    days, 3 cohorts, 16 variants, 1000 control replications), the Monte Carlo
    resolution was met, and the nonstationary control was blocked.  Anything
    less is ``PILOT_NOT_ACCEPTANCE`` with explicit blockers.

    ``checkpoint_path`` makes program identity explicit across retries: a
    checkpoint entry is reused only when its recorded identity matches the
    frozen generator/program/cohort/control labels and protocol parameters of
    this run.  Any mismatch forces a recompute of that program.
    """
    started = time.perf_counter()
    cpu_started = _cpu_seconds()
    generators = list(generators or GENERATORS)
    effects = [float(effect) for effect in (B6_EFFECTS if effects is None else effects)]
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
    full_generators = all(name in generators for name in REQUIRED_GENERATORS)
    full_effects = all(
        round(float(effect), 12) in {round(value, 12) for value in effects}
        for effect in REQUIRED_EFFECTS
    )

    null_tasks = [
        {
            "generator": name,
            "program": index,
            "cohorts": cohorts,
            "dev_days": dev_days,
            "fresh_days": fresh_days,
            "control_replications": control_replications,
            "family_size": family_size,
            "base_seed": base_seed,
        }
        for name in generators
        for index in range(programs)
    ]
    checkpoint = (
        load_checkpoint(checkpoint_path) if checkpoint_path else {"records": {}}
    )
    cached = checkpoint.get("records", {})
    records: list[dict] = []
    pending: list[dict] = []
    reused = 0
    for task in null_tasks:
        identity = program_identity(
            generator=task["generator"],
            program=task["program"],
            cohorts=cohorts,
            dev_days=dev_days,
            fresh_days=fresh_days,
            control_replications=control_replications,
            family_variants=family_size,
            max_candidates=B6_CANDIDATES_MAX_PER_COHORT,
            base_seed=base_seed,
        )
        entry = cached.get(identity)
        if (
            isinstance(entry, Mapping)
            and entry.get("identity") == identity
            and entry.get("signature")
            and entry.get("generator") == task["generator"]
            and int(entry.get("program", -1)) == int(task["program"])
        ):
            record = dict(entry)
            record["from_checkpoint"] = True
            records.append(record)
            reused += 1
        else:
            task["identity"] = identity
            pending.append(task)
    # One heavy workload at a time: null calibration first, then the power grid.
    fresh_records = _map(_run_worker, pending, workers)
    for record in fresh_records:
        record["from_checkpoint"] = False
    records.extend(fresh_records)
    if checkpoint_path:
        checkpoint_info = write_checkpoint(checkpoint_path, records)
        checkpoint_info.update({"reused": reused, "computed": len(fresh_records)})
    else:
        checkpoint_info = {"path": None, "reused": 0, "computed": len(fresh_records), "records": len(records)}
    by_generator = {name: [r for r in records if r["generator"] == name] for name in generators}
    reports = [_aggregate_generator(_spec(name), by_generator[name]) for name in generators]

    power_cohorts = cohorts if power_cohorts is None else power_cohorts
    power_report = _power_curve(
        generators=generators,
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
    coverage_present = (
        bool(reports)
        and all(r["coverage"].get("report_present", False) for r in reports)
        and all(r["coverage"]["status"] for r in reports)
    )
    power_present = bool(power_report["rows"]) and all(
        int(row.get("programs", 0)) > 0 for row in power_report["rows"]
    )
    guard = coverage_guard(
        reports=reports,
        power_rows=power_report["rows"],
        generators=generators,
        effects=effects,
    )
    nonstationary_ok = True
    for report in nonstationary_reports:
        blocked = report["coverage"]["status"] in (
            "FAILED_OR_BLOCKED_NONSTATIONARY",
            "BLOCKED",
            "FAILED_COVERAGE",
        )
        nonstationary_ok = nonstationary_ok and blocked and not report["certified_stationary"]

    resolution_met = bool(reports) and all(
        row["unresolved"] == 0 and row["unresolvable"] == 0
        for r in reports
        for row in r["cohort_alpha_spending"]
    )
    incomplete_selection = any(r["selection"]["incomplete"] for r in reports)
    full_workload = (
        full_programs
        and full_days
        and full_controls
        and full_cohorts
        and full_family
        and full_generators
        and full_effects
        and power_programs >= B6_NULL_PROGRAMS_PER_GENERATOR
    )

    # Cost basis: per-generator medians over programs actually executed in this
    # run (checkpoint-reused records keep earlier timings and are excluded when
    # fresh measurements exist).
    measured = [record for record in records if not record.get("from_checkpoint")]
    if not measured:
        measured = list(records)

    def _median(values) -> float:
        values = [float(value) for value in values]
        return float(np.median(values)) if values else 0.0

    per_generator_elapsed = {
        name: _median(
            record["elapsed_seconds"] for record in measured if record["generator"] == name
        )
        for name in generators
    }
    per_generator_cpu = {
        name: _median(
            record.get("cpu_seconds", 0.0)
            for record in measured
            if record["generator"] == name
        )
        for name in generators
    }
    measured_program_elapsed = _median(
        record["elapsed_seconds"] for record in measured
    )
    measured_program_cpu = _median(record.get("cpu_seconds", 0.0) for record in measured)
    programs_null_required = B6_NULL_PROGRAMS_PER_GENERATOR * len(generators)
    programs_power_required = (
        B6_NULL_PROGRAMS_PER_GENERATOR * len(generators) * len(effects)
    )
    core_seconds = sum(
        B6_NULL_PROGRAMS_PER_GENERATOR
        * (1 + len(effects))
        * max(per_generator_cpu[name], 0.0)
        for name in generators
    )
    estimated_full_wall_seconds = core_seconds / max(int(workers), 1)
    fits_budget = estimated_full_wall_seconds <= float(budget_seconds)

    blockers: list[str] = []
    if not full_workload or not fits_budget:
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
    if not guard["complete"]:
        blockers.append("COVERAGE_MATRIX_INCOMPLETE")

    acceptance = (
        full_workload
        and fits_budget
        and not blockers
        and fwer_target["pass"]
        and coverage_present
        and power_present
        and nonstationary_ok
        and resolution_met
        and not incomplete_selection
        and guard["complete"]
    )
    elapsed = time.perf_counter() - started
    peak_rss = _peak_rss_bytes()
    records_signature = hashlib.sha256(
        "".join(sorted(str(record.get("signature", "")) for record in records)).encode()
    ).hexdigest()

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
            "effects": effects,
            "checkpoint_path": checkpoint_path,
        },
        "protocol": {
            "seed_protocol": seed_protocol.PROTOCOL,
            "seed_protocol_hash": seed_protocol.PROTOCOL_HASH,
            "backend": seed_protocol.backend_info(),
            "decision_method": _DECISION_METHOD,
            "alpha_schedule": _ALPHA_SCHEDULE,
            "screening_family_variants": family_size,
            "decision_family": "ACTUAL_SELECTED_COHORT_NO_PADDING",
            "decision_family_sizes_max": B6_CANDIDATES_MAX_PER_COHORT,
            "required_resamples_by_family_size": {
                f"m{m}": {
                    f"j{j}": required_resamples(m, j)
                    for j in range(max(cohorts, 1))
                }
                for m in range(1, B6_CANDIDATES_MAX_PER_COHORT + 1)
            },
        },
        "generators": reports,
        "power_curve": power_report,
        "coverage_matrix": guard,
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
            "programs_resolved_with_actual_family_size": resolution_met,
        },
        "identity": {
            "records_signature": records_signature,
            "records": len(records),
            "unique_identities": len({record.get("identity") for record in records}),
            "checkpoint": checkpoint_info,
        },
        "compute_budget": {
            "pilot_wall_seconds": elapsed,
            "programs_completed": sum(r["programs"] for r in reports),
            "measured_program_elapsed_seconds_median": measured_program_elapsed,
            "measured_program_cpu_seconds_median": measured_program_cpu,
            "measured_per_generator_elapsed_seconds": per_generator_elapsed,
            "measured_per_generator_cpu_seconds": per_generator_cpu,
            "estimated_seconds_per_program": measured_program_elapsed,
            "programs_required": {
                "null": programs_null_required,
                "power": programs_power_required,
                "total": programs_null_required + programs_power_required,
            },
            "estimated_full_program_core_seconds": core_seconds,
            "estimated_full_program_core_hours": core_seconds / 3600.0,
            "estimated_full_program_wall_seconds": estimated_full_wall_seconds,
            "estimated_full_program_wall_hours": estimated_full_wall_seconds / 3600.0,
            "budget_seconds": float(budget_seconds),
            "fits_budget": fits_budget,
            "basis": "measured per-generator CPU seconds x required programs / workers",
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
            "cpu_seconds": _cpu_seconds() - cpu_started,
            "peak_rss_bytes": peak_rss,
            "workers": workers,
            "max_program_peak_rss_bytes": max(
                (r["resources"]["max_program_peak_rss_bytes"] for r in reports),
                default=0,
            ),
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
            f"family_sizes={row['selection']['family_size_histogram']} "
            f"zero_eligible_cohorts={row['insufficient_eligible_cohorts']} "
            f"certified_stationary={row['certified_stationary']}"
        )
    lines.append("")
    lines.append("cohort alpha spending (actual decision family size):")
    for row in report["generators"]:
        for alpha in row["cohort_alpha_spending"]:
            lines.append(
                f"  {row['generator']}: j={alpha['j']} alpha={alpha['alpha_frozen']} "
                f"family_sizes={alpha['family_sizes']} "
                f"b_required={alpha['b_required_by_family_size']} "
                f"max_resamples={alpha['max_resamples']} "
                f"unresolved={alpha['unresolved']}"
            )
    lines.append("")
    lines.append("power curve (injected variant 0):")
    for row in report["power_curve"]["rows"]:
        lines.append(
            f"  {row['generator']}: effect={row['effect_daily']} programs={row['programs']} "
            f"detections={row['detections']} power={row['power']} "
            f"mde_daily_median={row['mde_daily_median']}"
        )
    lines.append("")
    guard = report["coverage_matrix"]
    lines.append(
        f"coverage matrix complete={guard['complete']} problems={guard['problems']}"
    )
    lines.append("")
    lines.append(f"targets: {json.dumps(report['targets'], sort_keys=True)}")
    lines.append(f"blockers: {report['blockers']}")
    resources = report["resources"]
    lines.append(
        f"resources: wall={resources['wall_seconds']:.3f}s "
        f"cpu={resources['cpu_seconds']:.3f}s "
        f"peak_rss={resources['peak_rss_bytes']} B workers={resources['workers']}"
    )
    lines.append(
        f"compute budget: est_s_per_program={report['compute_budget']['estimated_seconds_per_program']:.3f} "
        f"est_full_core_hours={report['compute_budget']['estimated_full_program_core_hours']:.2f} "
        f"est_full_wall_hours={report['compute_budget']['estimated_full_program_wall_hours']:.2f} "
        f"fits_budget={report['compute_budget']['fits_budget']}"
    )
    lines.append(
        f"identity records_signature: {report['identity']['records_signature']}"
    )
    lines.append(f"artifact report_sha256: {report['artifact_hashes']['report_sha256']}")
    return "\n".join(lines) + "\n"
