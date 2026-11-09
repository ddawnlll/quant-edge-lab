"""Dependent statistics: HAC, stationary bootstrap, IUT, Holm and calibration."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from fractions import Fraction
from typing import Mapping, Sequence

import numpy as np

try:
    from scipy.stats import beta as _beta
    from scipy.stats import norm as _norm
except Exception:  # pragma: no cover
    _beta = None
    _norm = None


def hac_se(x: Sequence[float], lag: int | None = None) -> float:
    arr = np.asarray(x, dtype=float)
    n = len(arr)
    if n < 3:
        raise ValueError("INSUFFICIENT_HISTORY")
    if lag is None:
        lag = int(math.floor(4 * (n / 100.0) ** (2.0 / 9.0)))
    mean = arr.mean()
    z = arr - mean
    gamma0 = float(np.dot(z, z) / n)
    var = gamma0
    for j in range(1, min(lag, n - 1) + 1):
        w = 1.0 - j / (lag + 1.0)
        gamma = float(np.dot(z[j:], z[:-j]) / n)
        var += 2.0 * w * gamma
    if var <= 0:
        raise ValueError("DEGENERATE_VARIANCE")
    return math.sqrt(var / n)


def stationary_bootstrap_indices(n: int, block: int, rng: np.random.Generator) -> np.ndarray:
    if n <= 0 or block <= 0:
        raise ValueError("INVALID_DATA")
    p = 1.0 / block
    out = np.empty(n, dtype=np.int64)
    i = int(rng.integers(0, n))
    for t in range(n):
        if t > 0 and rng.random() >= p:
            i = (i + 1) % n
        else:
            i = int(rng.integers(0, n))
        out[t] = i
    return out


@dataclass
class BootstrapResult:
    p_raw: float
    p_upper: float
    t_obs: float
    se: float
    b: int
    method_hash: str


def studentized_bootstrap_p(
    x: Sequence[float],
    b: int = 2000,
    block: int = 10,
    seed: int = 0,
    batch: int = 64,
) -> BootstrapResult:
    """One-sided null-centred studentized stationary bootstrap.

    T* = mean(z*)/SE_HAC(z*) with z=x-mean(x); the original SE is never
    copied into every replicate. Batches bound peak memory.
    """
    arr = np.asarray(x, dtype=float)
    n = len(arr)
    if n < 3:
        raise ValueError("INSUFFICIENT_HISTORY")
    se = hac_se(arr)
    t_obs = float(arr.mean() / se)
    rng = np.random.default_rng(seed)
    z = arr - arr.mean()
    k = 0
    done = 0
    attempts = 0
    max_attempts = b * 20
    while done < b and attempts < max_attempts:
        chunk = min(batch, b - done)
        for _ in range(chunk):
            attempts += 1
            idx = stationary_bootstrap_indices(n, block, rng)
            sample = z[idx]
            try:
                se_star = hac_se(sample)
            except ValueError:
                continue
            if se_star == 0:
                continue
            t_star = float(sample.mean() / se_star)
            if t_star >= t_obs:
                k += 1
            done += 1
    if done < b:
        raise ValueError("BOOTSTRAP_DEGENERATE")
    m = b
    p_raw = (k + 1) / (m + 1)
    p_upper = p_raw
    if _beta is not None:
        error = 0.01 / (2.0 * m)
        q_upper = float(_beta.ppf(1.0 - error, k + 1, m - k)) if k < m else 1.0
        p_upper = max(p_raw, (1.0 + m * q_upper) / (m + 1))
    return BootstrapResult(p_raw=p_raw, p_upper=p_upper, t_obs=t_obs, se=se, b=m, method_hash="SB_HAC_STUDENTIZED_V1")


def iut(p_profit: float, p_control: float) -> float:
    return max(p_profit, p_control)


def holm(pvals: Sequence[float], alpha: float) -> list[bool]:
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    rejected = [False] * m
    for rank, idx in enumerate(order):
        threshold = alpha / (m - rank)
        if pvals[idx] <= threshold:
            rejected[idx] = True
        else:
            break
    return rejected


def classify(p: float, power: float, ci_low: float, ci_high: float, economic_minimum: float) -> str:
    if p >= 0.05 or power < 0.20:
        return "INSUFFICIENT_EVIDENCE"
    if ci_low > economic_minimum:
        return "EDGE"
    return "NO_EDGE"


def minimum_b(m: int, j: int, max_resamples: int) -> tuple[int, bool]:
    alpha = 0.05 / (2 ** (j + 1))
    required = math.ceil(20 * m / alpha) - 1
    return required, required + 1 <= max_resamples


def walk_forward_splits(
    dates: Sequence[str], train_days: int, test_days: int, purge_days: int, embargo_days: int
) -> list[dict]:
    """Calendar walk-forward windows with equal-boundary purging."""
    if train_days <= 0 or test_days <= 0 or purge_days < 0 or embargo_days < 0:
        raise ValueError("INVALID_DATA")
    splits = []
    start = 0
    while start + train_days + purge_days + test_days <= len(dates):
        train_end = start + train_days
        test_start = train_end + purge_days
        test_end = test_start + test_days
        splits.append(
            {
                "train_start": dates[start],
                "train_end": dates[train_end - 1],
                "test_start": dates[test_start],
                "test_end": dates[test_end - 1],
                "purge_days": purge_days,
                "embargo_days": embargo_days,
            }
        )
        start = test_end + embargo_days
    if not splits:
        raise ValueError("INSUFFICIENT_HISTORY")
    return splits


def ar1_series(n: int, phi: float, sigma: float, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    eps = rng.normal(0.0, sigma, size=n)
    out = np.empty(n)
    out[0] = eps[0]
    for i in range(1, n):
        out[i] = phi * out[i - 1] + eps[i]
    return out


def stationarity_screen(series: np.ndarray, threshold: float = 3.0) -> bool:
    """Reject a program whose first/second half means differ materially.

    A regime break is reported as a blocked/failed program, not silently
    promoted as an edge.
    """
    n = len(series)
    if n < 20:
        return False
    first, second = series[: n // 2], series[n // 2 :]
    diff = abs(first.mean() - second.mean())
    scale = hac_se(series)
    return diff / scale <= threshold


def null_pipeline(series: np.ndarray, m: int = 4, controls: int = 2, alpha: float = 0.025, b: int = 200, seed: int = 0, effect: float = 0.0) -> bool:
    """One full null program: screen m candidates + controls, IUT, Holm."""
    if not stationarity_screen(series):
        return False
    rng = np.random.default_rng(seed)
    pvals = []
    for i in range(m):
        candidate = series + effect + rng.normal(0, 0.05, size=len(series))
        control = rng.normal(0, 1, size=len(series))
        try:
            p_c = studentized_bootstrap_p(candidate, b=b, seed=seed + i, batch=32).p_upper
            p_ctrl = studentized_bootstrap_p(control, b=b, seed=seed + 100 + i, batch=32).p_upper
        except ValueError:
            return False
        pvals.append(iut(p_c, p_ctrl))
    rejected = holm(pvals, alpha)
    return any(rejected)


def null_generator(name: str, n: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    if name == "ar1":
        phi = 0.2 + 0.6 * (seed % 5) / 4.0
        return ar1_series(n, phi, 1.0, seed)
    if name == "t5":
        from scipy.stats import t

        phi = 0.4
        eps = t.rvs(5, size=n, random_state=rng)
        eps = eps / eps.std()
        out = np.empty(n)
        out[0] = eps[0]
        for i in range(1, n):
            out[i] = phi * out[i - 1] + eps[i]
        return out
    if name == "common_factor":
        factor = ar1_series(n, 0.3, 1.0, seed)
        idio = rng.normal(0, 1, n)
        return 0.7 * factor + 0.3 * idio
    if name == "regime_shift":
        first = rng.normal(0.0, 1.0, n // 2)
        second = rng.normal(0.9, 1.2, n - n // 2)
        return np.concatenate([first, second])
    raise ValueError(f"unknown null generator {name}")


def _power_program(args: tuple[float, int, int, int, int, int]) -> bool:
    effect, i, n, m, b, seed = args
    series = null_generator("ar1", n, seed + i)
    return null_pipeline(series, m=m, b=b, seed=seed + i, effect=effect)


def power_grid(
    effects: list[float] | None = None,
    programs: int = 30,
    n: int = 240,
    m: int = 4,
    b: int = 500,
    seed: int = 0,
    workers: int | None = None,
) -> dict:
    import os
    from concurrent.futures import ProcessPoolExecutor

    effects = effects or [0.0, 0.2, 0.4, 0.6]
    if workers is None:
        workers = min(6, os.cpu_count() or 1)
    rows = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for effect in effects:
            tasks = [(effect, i, n, m, b, seed) for i in range(programs)]
            promoted = sum(1 for result in pool.map(_power_program, tasks) if result)
            rows.append({"effect_daily": effect, "programs": programs, "promotions": promoted, "power": promoted / programs})
    return {"status": "PASS", "rows": rows, "note": "local injected-effect power grid; not a full MDE/coverage certificate"}


def _null_program(args: tuple[str, int, int, int, int, int]) -> bool:
    generator, i, n, m, b, seed = args
    series = null_generator(generator, n, seed + i)
    return null_pipeline(series, m=m, b=b, seed=seed + i)


def calibrate_generator(
    generator: str,
    programs: int = 200,
    n: int = 120,
    m: int = 4,
    b: int = 200,
    seed: int = 0,
    workers: int | None = None,
) -> dict:
    """Estimate the false promotion rate on dependent null programs.

    This is a bounded local calibration, not the full B6 four-generator run.
    Programs are independent and run in a bounded process pool.
    """
    import os
    from concurrent.futures import ProcessPoolExecutor

    if workers is None:
        workers = min(6, os.cpu_count() or 1)
    tasks = [(generator, i, n, m, b, seed) for i in range(programs)]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        promoted = sum(1 for result in pool.map(_null_program, tasks) if result)
    rate = promoted / programs
    # 95% exact Clopper-Pearson upper bound.
    if _beta is not None:
        upper = float(_beta.ppf(0.95, promoted + 1, max(programs - promoted, 1))) if promoted < programs else 1.0
    else:  # conservative fallback
        upper = min(1.0, (promoted + 1) / max(programs, 1) + 0.05)
    return {
        "generator": generator,
        "programs": programs,
        "promotions": promoted,
        "false_promotion_rate": rate,
        "binomial_upper_95": upper,
        "threshold": 0.075,
        "pass": upper <= 0.075,
    }


def calibrate_nulls(
    programs: int = 200,
    generators: list[str] | None = None,
    n: int = 120,
    m: int = 4,
    b: int = 200,
    seed: int = 0,
    workers: int | None = None,
) -> dict:
    generators = generators or ["ar1", "t5", "common_factor", "regime_shift"]
    reports = [calibrate_generator(g, programs, n, m, b, seed, workers) for g in generators]
    return {
        "status": "PASS" if all(r["pass"] for r in reports) else "FAIL",
        "generators": reports,
        "threshold": 0.075,
        "note": "full four-generator local B6 calibration" if programs >= 1000 else "bounded local calibration",
    }


# ---------------------------------------------------------------------------
# Paired decision procedure (TASK-14 / F27 / F28)
#
# Frozen protocol:
#   * one shared UTC daily calendar and one shared initial-capital denominator
#     for candidate and matched control;
#   * one stationary-bootstrap index vector per replicate, applied to both
#     endpoints (and therefore to every symbol in the panel);
#   * HAC standard error recomputed inside every replicate (never the observed
#     SE copied into every replicate);
#   * endpoint A = candidate mean net return vs the economic floor,
#     endpoint B = (candidate - matched control) mean vs the control floor;
#   * intersection-union test (max of the two one-sided MC-upper p-values) and
#     Holm step-down over the fixed preregistered family;
#   * conservative Clopper-Pearson Monte Carlo upper bound, and a hard
#     WAITING_COMPUTE_BUDGET state when the frozen minimum B cannot fit inside
#     the declared resample envelope. Alpha is never relaxed.
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DECISION_METHOD = "SB_HAC_STUDENTIZED_SHARED_INDEX_IUT_HOLM_V1"
_SENSITIVITY_SEED_OFFSET = 1_000_003

_VERDICT_PROMOTE = "PROMOTE"
_VERDICT_NO_PROMOTION = "NO_PROMOTION"
_VERDICT_INSUFFICIENT = "INSUFFICIENT_EVIDENCE"
_VERDICT_WAITING = "WAITING_COMPUTE_BUDGET"


class DecisionError(ValueError):
    """The supplied observations cannot support a paired decision."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _decimal(value, field: str) -> Fraction:
    """Parse a caller-supplied decimal string without binary-float drift."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise DecisionError("INVALID_DATA", f"{field} must be a decimal string")
    try:
        return Fraction(str(value))
    except (ValueError, ZeroDivisionError) as exc:
        raise DecisionError("INVALID_DATA", f"{field} is not a decimal: {value!r}") from exc


def _safe_hac(x: np.ndarray, lag: int | None) -> float:
    try:
        return hac_se(x, lag=lag)
    except ValueError as exc:
        code = str(exc)
        if code in ("DEGENERATE_VARIANCE", "INSUFFICIENT_HISTORY"):
            raise DecisionError(code, "HAC standard error is undefined") from exc
        raise


def _frozen_alpha(family_index: int) -> float:
    if family_index < 0:
        raise DecisionError("INVALID_DATA", "family_index must be >= 0")
    return 0.05 / (2.0 ** (family_index + 1))


def required_resamples(family_size: int, family_index: int = 0) -> int:
    """Frozen B requirement: B+1 >= max(10000, ceil(20m/alpha_j))."""
    if family_size < 1:
        raise DecisionError("INVALID_DATA", "family_size must be >= 1")
    if family_index < 0:
        raise DecisionError("INVALID_DATA", "family_index must be >= 0")
    # alpha_j = 1 / (20 * 2**(j+1)), so 20*m/alpha_j = 400*m*2**(j+1) exactly.
    return max(10000, 400 * family_size * 2 ** (family_index + 1)) - 1


def mc_upper_p(k: int, b: int, family_size: int = 1) -> float:
    """Conservative one-sided Monte Carlo upper bound for a bootstrap p-value."""
    if b < 1 or k < 0 or k > b:
        raise DecisionError("INVALID_DATA", "invalid bootstrap count")
    p_raw = (k + 1) / (b + 1)
    if _beta is None:  # pragma: no cover - scipy is a declared dependency
        return min(1.0, p_raw + 0.05)
    error = 0.01 / (2.0 * family_size)
    q_upper = float(_beta.ppf(1.0 - error, k + 1, b - k)) if k < b else 1.0
    return max(p_raw, (1.0 + b * q_upper) / (b + 1))


def two_half_diagnostic(
    series: Sequence[float],
    *,
    lag: int | None = None,
    threshold: float = 3.29,
    variance_ratio_max: float = 4.0,
    min_half: int = 50,
) -> dict:
    """Frozen two-half stationarity alarm.

    A PASS is a design diagnostic, not a stationarity proof. A FAIL blocks the
    program instead of presenting the regime break as an edge.
    """
    arr = np.asarray(series, dtype=float)
    n = int(arr.size)
    if n < 2 * min_half:
        return {
            "status": "INSUFFICIENT",
            "n": n,
            "n_first": n // 2,
            "n_second": n - n // 2,
            "min_half": min_half,
            "reason": "INSUFFICIENT_N",
        }
    first, second = arr[: n // 2], arr[n // 2 :]
    try:
        se_first = hac_se(first, lag=lag)
        se_second = hac_se(second, lag=lag)
    except ValueError as exc:
        return {"status": "INSUFFICIENT", "n": n, "min_half": min_half, "reason": str(exc)}
    denominator = math.hypot(se_first, se_second)
    if denominator <= 0:  # pragma: no cover - hac_se already enforces > 0
        return {"status": "INSUFFICIENT", "n": n, "min_half": min_half, "reason": "DEGENERATE_VARIANCE"}
    statistic = abs(float(first.mean()) - float(second.mean())) / denominator
    small, large = sorted((se_first, se_second))
    variance_ratio = (large * large) / (small * small)
    stationary = statistic <= threshold and variance_ratio <= variance_ratio_max
    return {
        "status": "PASS" if stationary else "FAIL",
        "n": n,
        "n_first": int(first.size),
        "n_second": int(second.size),
        "min_half": min_half,
        "statistic": statistic,
        "variance_ratio": variance_ratio,
        "threshold": threshold,
        "variance_ratio_max": variance_ratio_max,
    }


def holm_adjusted(pvals: Sequence[float], alpha: float) -> dict:
    """Holm step-down with adjusted p-values and per-rank thresholds."""
    m = len(pvals)
    if m == 0:
        return {"alpha": alpha, "m": 0, "pvalues": [], "adjusted": [], "thresholds": [], "rejected": []}
    for p in pvals:
        if not isinstance(p, (int, float)) or not math.isfinite(float(p)) or not (0.0 <= float(p) <= 1.0):
            raise DecisionError("INVALID_DATA", f"p-value outside [0,1]: {p!r}")
    order = sorted(range(m), key=lambda i: pvals[i])
    adjusted = [1.0] * m
    thresholds = [0.0] * m
    rejected = [False] * m
    running = 0.0
    failed = False
    for rank, idx in enumerate(order):
        factor = m - rank
        running = min(1.0, max(running, factor * float(pvals[idx])))
        adjusted[idx] = running
        thresholds[idx] = alpha / factor
        if not failed and float(pvals[idx]) <= thresholds[idx]:
            rejected[idx] = True
        else:
            failed = True
    return {
        "alpha": alpha,
        "m": m,
        "pvalues": [float(p) for p in pvals],
        "adjusted": adjusted,
        "thresholds": thresholds,
        "rejected": rejected,
        "order": order,
    }


def _prepare_series(candidate, control, capital) -> dict:
    """Validate panels and build the single shared calendar and return series.

    ``{symbol: {UTC date: net PnL decimal string}}`` for both endpoints.
    Every symbol must cover exactly the shared calendar: a zero-trade day is an
    explicit ``"0"``, a missing mark is MISSING_DATA and is never filled.
    """
    cap = _decimal(capital, "capital")
    if cap <= 0:
        raise DecisionError("INVALID_DATA", "capital must be positive")
    panels: dict[str, dict[str, dict[str, Fraction]]] = {}
    for label, panel in (("candidate", candidate), ("control", control)):
        if not isinstance(panel, Mapping) or len(panel) == 0:
            raise DecisionError("MISSING_DATA", f"{label} panel is empty")
        cleaned: dict[str, dict[str, Fraction]] = {}
        for symbol, rows in panel.items():
            if not isinstance(symbol, str) or not symbol:
                raise DecisionError("INVALID_DATA", f"{label} symbol id must be a non-empty string")
            if not isinstance(rows, Mapping) or len(rows) == 0:
                raise DecisionError("MISSING_DATA", f"{label}/{symbol} has no daily observations")
            cleaned_rows: dict[str, Fraction] = {}
            for day, value in rows.items():
                if not isinstance(day, str) or _DATE_RE.match(day) is None:
                    raise DecisionError("INVALID_DATA", f"{label}/{symbol} date is not a UTC YYYY-MM-DD: {day!r}")
                cleaned_rows[day] = _decimal(value, f"{label}/{symbol}/{day}")
            cleaned[symbol] = cleaned_rows
        panels[label] = cleaned

    calendar = sorted({day for panel in panels.values() for rows in panel.values() for day in rows})
    if len(calendar) < 3:
        raise DecisionError("INSUFFICIENT_HISTORY", f"only {len(calendar)} shared calendar days")

    totals: dict[str, np.ndarray] = {}
    zero_days: dict[str, int] = {}
    for label, panel in panels.items():
        day_total = {day: Fraction(0) for day in calendar}
        for symbol, rows in panel.items():
            missing = [day for day in calendar if day not in rows]
            if missing:
                raise DecisionError(
                    "MISSING_DATA",
                    f"{label}/{symbol} missing {len(missing)} of {len(calendar)} shared calendar days (first {missing[0]})",
                )
            for day in calendar:
                day_total[day] += rows[day]
        totals[label] = np.array([float(day_total[day] / cap) for day in calendar], dtype=float)
        zero_days[label] = sum(1 for day in calendar if day_total[day] == 0)

    from ..contract.canonical import hash_jcs

    return {
        "calendar": calendar,
        "calendar_hash": hash_jcs({"calendar": calendar}),
        "capital": cap,
        "candidate": totals["candidate"],
        "control": totals["control"],
        "zero_trade_days_candidate": zero_days["candidate"],
        "zero_trade_days_control": zero_days["control"],
        "n_symbols_candidate": len(panels["candidate"]),
        "n_symbols_control": len(panels["control"]),
    }


def _method_hash(params: dict) -> str:
    from ..contract.canonical import hash_jcs

    payload = {"method": _DECISION_METHOD, **params}
    return hash_jcs(payload)


def _stationary_indices_vectorized(n: int, block: int, rng: np.random.Generator) -> np.ndarray:
    """Vectorized stationary-bootstrap index path for the decision loop.

    Same block process as :func:`stationary_bootstrap_indices` (probability
    ``1/block`` of a fresh uniform start, otherwise advance by one), but the
    per-step Python RNG calls are replaced by two uniform draws per index so a
    bounded decision run stays inside its compute envelope. The historical
    reference stream used by the B6 calibration is left untouched.
    """
    if n <= 0 or block <= 0:
        raise ValueError("INVALID_DATA")
    p = 1.0 / block
    draws = rng.random(2 * n)
    fresh = np.empty(n, dtype=bool)
    fresh[0] = True
    if n > 1:
        fresh[1:] = draws[1:n] < p
    starts = (draws[n:] * n).astype(np.int64)
    positions = np.arange(n, dtype=np.int64)
    block_start = np.maximum.accumulate(np.where(fresh, positions, -1))
    return (starts[block_start] + (positions - block_start)) % n


def _evaluate_block(
    cand: np.ndarray,
    ctrl: np.ndarray,
    floor_a: float,
    floor_b: float,
    *,
    block: int,
    b: int,
    hac_lag: int,
    seed: int,
    batch: int,
    family_size: int,
    alpha: float,
    power_target: float,
) -> dict:
    """One block-length evaluation with shared bootstrap indices per replicate."""
    n = int(cand.size)
    difference = cand - ctrl
    mean_c = float(cand.mean())
    mean_k = float(ctrl.mean())
    mean_d = float(difference.mean())
    # Null-centred series: the bootstrap distribution of T* is centred at zero,
    # while the observed statistic carries the economic floor.
    z_a = cand - mean_c
    z_k = ctrl - mean_k
    z_b = difference - mean_d
    se_a = _safe_hac(z_a, hac_lag)
    se_b = _safe_hac(z_b, hac_lag)
    t_a = (mean_c - floor_a) / se_a
    t_b = (mean_d - floor_b) / se_b
    z_alpha = float(_norm.ppf(1.0 - alpha)) if _norm is not None else 1.959963984540054
    z_power = float(_norm.ppf(power_target)) if _norm is not None else 0.8416212335729143

    # Bounded scalar storage: two pivot arrays of length b, never b x n.
    piv_a = np.empty(b, dtype=float)
    piv_b = np.empty(b, dtype=float)
    k_a = k_b = 0
    done = 0
    attempts = 0
    max_attempts = b * 20
    rng = np.random.default_rng(seed)
    while done < b and attempts < max_attempts:
        chunk = min(batch, b - done)
        for _ in range(chunk):
            attempts += 1
            idx = _stationary_indices_vectorized(n, block, rng)
            # One index vector, applied to candidate and control alike.
            xb = z_a[idx]
            xk = z_k[idx]
            zb = xb - xk
            try:
                sa = hac_se(xb, lag=hac_lag)
                sb = hac_se(zb, lag=hac_lag)
            except ValueError:
                # A degenerate replicate is dropped, never imputed as p=0/1.
                continue
            mean_xb = float(xb.mean())
            mean_zb = float(zb.mean())
            # z is already null-centred, so the pivot is mean(z*)/SE(z*).
            piv_a[done] = mean_xb / sa
            piv_b[done] = mean_zb / sb
            if mean_xb / sa >= t_a:
                k_a += 1
            if mean_zb / sb >= t_b:
                k_b += 1
            done += 1
    if done < b:
        raise DecisionError("BOOTSTRAP_DEGENERATE", f"only {done} of {b} replicates were non-degenerate")

    q_a025, q_a05, q_a95, q_a975 = (float(v) for v in np.quantile(piv_a, [0.025, 0.05, 0.95, 0.975]))
    q_b025, q_b05, q_b95, q_b975 = (float(v) for v in np.quantile(piv_b, [0.025, 0.05, 0.95, 0.975]))

    def endpoint(name: str, mean: float, se: float, t_obs: float, k: int, floor: float,
                 q025: float, q05: float, q95: float, q975: float) -> dict:
        t_shift = t_obs - z_alpha
        if _norm is not None:
            power = float(_norm.cdf(t_shift))
        else:  # pragma: no cover - scipy is a declared dependency
            power = 0.5 * (1.0 + math.erf(t_shift / math.sqrt(2.0)))
        return {
            "name": name,
            "floor": floor,
            "mean_daily_return": mean,
            "hac_se": se,
            "t_obs": t_obs,
            "p_raw": (k + 1) / (b + 1),
            "p_upper": mc_upper_p(k, b, family_size),
            "ci_lower_95": mean - q95 * se,
            "ci_upper_95": mean - q05 * se,
            "ci_low_two_sided": mean - q975 * se,
            "ci_high_two_sided": mean - q025 * se,
            "power": power,
            "mde_daily": (z_alpha + z_power) * se,
            "negative_evidence": (mean - q05 * se) < floor,
        }

    endpoint_a = endpoint("A", mean_c, se_a, t_a, k_a, floor_a, q_a025, q_a05, q_a95, q_a975)
    endpoint_b = endpoint("B", mean_d, se_b, t_b, k_b, floor_b, q_b025, q_b05, q_b95, q_b975)
    return {
        "block": block,
        "seed": seed,
        "b": b,
        "mean_control": mean_k,
        "endpoints": {"A": endpoint_a, "B": endpoint_b},
        "iut": {
            "p_raw": max(endpoint_a["p_raw"], endpoint_b["p_raw"]),
            "p_upper": max(endpoint_a["p_upper"], endpoint_b["p_upper"]),
        },
    }


def _evaluate_all(
    prep: dict,
    floor_a: float,
    floor_b: float,
    *,
    alpha: float,
    block: int,
    block_sensitivity: int,
    hac_lag: int,
    b: int,
    seed: int,
    batch: int,
    family_size: int,
    power_target: float,
    min_half: int = 50,
) -> dict:
    cand = prep["candidate"]
    ctrl = prep["control"]
    diag_a = two_half_diagnostic(cand, lag=hac_lag, min_half=min_half)
    diag_b = two_half_diagnostic(cand - ctrl, lag=hac_lag, min_half=min_half)
    statuses = {diag_a["status"], diag_b["status"]}
    if "FAIL" in statuses:
        stationarity_status = "FAIL"
    elif "INSUFFICIENT" in statuses:
        stationarity_status = "INSUFFICIENT"
    else:
        stationarity_status = "PASS"
    stationarity = {
        "status": stationarity_status,
        "candidate": diag_a,
        "difference": diag_b,
    }
    if diag_a.get("reason") == "DEGENERATE_VARIANCE" or diag_b.get("reason") == "DEGENERATE_VARIANCE":
        raise DecisionError("DEGENERATE_VARIANCE", "series has no usable variance")
    if stationarity_status != "PASS":
        # A regime break or an unusable diagnostic blocks the program before
        # any bootstrap compute is spent; it is never promoted.
        return {
            "computable": False,
            "primary": None,
            "sensitivity": None,
            "stationarity": stationarity,
        }
    primary = _evaluate_block(
        cand, ctrl, floor_a, floor_b,
        block=block, b=b, hac_lag=hac_lag, seed=seed, batch=batch,
        family_size=family_size, alpha=alpha, power_target=power_target,
    )
    sensitivity = _evaluate_block(
        cand, ctrl, floor_a, floor_b,
        block=block_sensitivity, b=b, hac_lag=hac_lag,
        seed=seed + _SENSITIVITY_SEED_OFFSET, batch=batch,
        family_size=family_size, alpha=alpha, power_target=power_target,
    )
    return {
        "computable": True,
        "primary": primary,
        "sensitivity": sensitivity,
        "stationarity": stationarity,
    }


def _resolve_verdict(
    stationarity_status: str,
    *,
    resolved: bool,
    rejected: bool,
    power_a: float,
    power_b: float,
    power_target: float,
    negative_a: bool,
    negative_b: bool,
) -> tuple[str, list[str]]:
    if stationarity_status == "FAIL":
        return _VERDICT_INSUFFICIENT, ["NONSTATIONARY"]
    if stationarity_status == "INSUFFICIENT":
        return _VERDICT_INSUFFICIENT, ["STATIONARITY_DIAGNOSTIC_INSUFFICIENT"]
    if rejected and resolved and power_a >= power_target and power_b >= power_target:
        return _VERDICT_PROMOTE, ["IUT_REJECTED", "HOLM_REJECTED", "POWER_OK"]
    if resolved and (negative_a or negative_b):
        reasons = []
        if negative_a:
            reasons.append("NEGATIVE_EVIDENCE_ENDPOINT_A")
        if negative_b:
            reasons.append("NEGATIVE_EVIDENCE_ENDPOINT_B")
        return _VERDICT_NO_PROMOTION, reasons
    reasons = []
    if not resolved:
        reasons.append("B_BELOW_FROZEN_MINIMUM")
    if not rejected:
        reasons.append("IUT_NOT_REJECTED")
    if min(power_a, power_b) < power_target:
        reasons.append("LOW_POWER")
    return _VERDICT_INSUFFICIENT, reasons


def _budget_view(b_requested: int, b_used: int, max_resamples: int, required: int, block: int,
                 block_sensitivity: int, seed: int, family_size: int, family_index: int) -> dict:
    return {
        "b_requested": b_requested,
        "b_used": b_used,
        "b_required": required,
        "max_resamples": max_resamples,
        "resolved": b_used >= required,
        "unresolvable": required + 1 > max_resamples,
        "block": block,
        "sensitivity_block": block_sensitivity,
        "seed": seed,
        "family_size": family_size,
        "family_index": family_index,
        "alpha_schedule": "0.05 / 2^(j+1)",
    }


def _validate_protocol(alpha: float, block: int, block_sensitivity: int, hac_lag: int, b: int,
                       max_resamples: int, family_size: int, family_index: int, power_target: float,
                       batch: int) -> float:
    frozen = _frozen_alpha(family_index)
    if not isinstance(alpha, (int, float)) or not (0.0 < float(alpha) <= frozen):
        raise DecisionError("ALPHA_NOT_PREREGISTERED", f"alpha must be in (0, {frozen}]")
    if family_size < 1:
        raise DecisionError("INVALID_DATA", "family_size must be >= 1")
    for name, value, minimum in (
        ("block", block, 1),
        ("block_sensitivity", block_sensitivity, 1),
        ("hac_lag", hac_lag, 0),
        ("b", b, 1),
        ("max_resamples", max_resamples, 1),
        ("batch", batch, 1),
    ):
        if not isinstance(value, int) or value < minimum:
            raise DecisionError("INVALID_DATA", f"{name} must be an integer >= {minimum}")
    if not isinstance(power_target, (int, float)) or not (0.0 < float(power_target) < 1.0):
        raise DecisionError("INVALID_DATA", "power_target must be in (0, 1)")
    return frozen


def _holm_view(p_self: float, candidate_id: str, family_pvalues, family_size: int, alpha: float) -> dict:
    family: dict[str, float] = {}
    if family_pvalues is not None:
        if not isinstance(family_pvalues, Mapping):
            raise DecisionError("INVALID_DATA", "family_pvalues must be a mapping of candidate id to p-value")
        for cid, p in family_pvalues.items():
            if str(cid) == candidate_id:
                continue
            family[str(cid)] = float(p)
    if len(family) + 1 > family_size:
        raise DecisionError("INVALID_DATA", "family_pvalues exceeds the fixed family size")
    family[candidate_id] = float(p_self)
    padded = 0
    while len(family) < family_size:
        family[f"UNSPECIFIED-{padded:02d}"] = 1.0
        padded += 1
    ids = sorted(family)
    table = holm_adjusted([family[cid] for cid in ids], alpha)
    position = ids.index(candidate_id)
    return {
        "candidate_id": candidate_id,
        "family_size": family_size,
        "members": ids,
        "pvalues": table["pvalues"],
        "adjusted": table["adjusted"],
        "thresholds": table["thresholds"],
        "rejected": table["rejected"],
        "self_adjusted_p": table["adjusted"][position],
        "self_threshold": table["thresholds"][position],
        "self_rejected": table["rejected"][position],
        "padded_members": padded,
        "alpha": alpha,
    }


def _endpoint_payload(
    prep: dict,
    floor_a: Fraction,
    floor_b: Fraction,
    evaluated: dict,
    *,
    alpha: float,
    frozen: float,
    block: int,
    block_sensitivity: int,
    hac_lag: int,
    b: int,
    b_used: int,
    max_resamples: int,
    seed: int,
    family_size: int,
    family_index: int,
    power_target: float,
) -> dict:
    required = required_resamples(family_size, family_index)
    method_hash = _method_hash(
        {
            "block": block,
            "block_sensitivity": block_sensitivity,
            "hac_lag": hac_lag,
            "b": b_used,
            "max_resamples": max_resamples,
            "seed": seed,
            "alpha": float(alpha),
            "family_size": family_size,
            "family_index": family_index,
            "power_target": float(power_target),
            "economic_floor": str(floor_a),
            "control_floor": str(floor_b),
            "capital": str(prep["capital"]),
        }
    )
    return {
        "status": "PASS",
        "method": _DECISION_METHOD,
        "method_hash": method_hash,
        "calendar_hash": prep["calendar_hash"],
        "n_days": len(prep["calendar"]),
        "first_day": prep["calendar"][0],
        "last_day": prep["calendar"][-1],
        "n_symbols_candidate": prep["n_symbols_candidate"],
        "n_symbols_control": prep["n_symbols_control"],
        "zero_trade_days_candidate": prep["zero_trade_days_candidate"],
        "zero_trade_days_control": prep["zero_trade_days_control"],
        "capital": str(prep["capital"]),
        "floors": {"economic": str(float(floor_a)), "control": str(float(floor_b))},
        "alpha": {
            "nominal": float(alpha),
            "frozen": frozen,
            "family_size": family_size,
            "family_index": family_index,
            "schedule": "0.05 / 2^(j+1)",
        },
        "budget": _budget_view(
            b, b_used, max_resamples, required, block, block_sensitivity,
            seed, family_size, family_index,
        ),
        "computable": evaluated.get("computable", True),
        "primary": evaluated["primary"],
        "sensitivity": evaluated["sensitivity"],
        "stationarity": evaluated["stationarity"],
    }


def paired_endpoints(
    candidate,
    control,
    capital,
    economic_floor,
    control_floor,
    *,
    alpha: float = 0.025,
    block: int = 5,
    block_sensitivity: int = 10,
    hac_lag: int = 5,
    b: int = 2000,
    max_resamples: int = 1_000_000,
    seed: int = 0,
    family_size: int = 1,
    family_index: int = 0,
    power_target: float = 0.8,
    batch: int = 64,
    min_half: int = 50,
) -> dict:
    """Compute both paired endpoints under the frozen dependent-statistics protocol.

    This is the statistics half of the decision: it validates the shared
    calendar, runs the shared-index bootstrap for the primary and sensitivity
    block lengths, and reports both endpoints, CIs, power/MDE, stationarity and
    the compute budget. It never returns a promotion verdict.
    """
    frozen = _validate_protocol(
        alpha, block, block_sensitivity, hac_lag, b, max_resamples,
        family_size, family_index, power_target, batch,
    )
    floor_a = _decimal(economic_floor, "economic_floor")
    floor_b = _decimal(control_floor, "control_floor")
    prep = _prepare_series(candidate, control, capital)
    b_used = min(b, max_resamples)
    evaluated = _evaluate_all(
        prep, float(floor_a), float(floor_b),
        alpha=float(alpha), block=block, block_sensitivity=block_sensitivity,
        hac_lag=hac_lag, b=b_used, seed=seed, batch=batch,
        family_size=family_size, power_target=power_target, min_half=min_half,
    )
    return _endpoint_payload(
        prep, floor_a, floor_b, evaluated,
        alpha=alpha, frozen=frozen, block=block,
        block_sensitivity=block_sensitivity, hac_lag=hac_lag, b=b,
        b_used=b_used, max_resamples=max_resamples, seed=seed,
        family_size=family_size, family_index=family_index,
        power_target=power_target,
    )


def paired_decision(
    candidate,
    control,
    capital,
    economic_floor,
    control_floor,
    *,
    alpha: float = 0.025,
    block: int = 5,
    block_sensitivity: int = 10,
    hac_lag: int = 5,
    b: int = 2000,
    max_resamples: int = 1_000_000,
    seed: int = 0,
    family_size: int = 1,
    family_index: int = 0,
    family_pvalues: Mapping[str, float] | None = None,
    candidate_id: str = "CANDIDATE",
    power_target: float = 0.8,
    batch: int = 64,
    min_half: int = 50,
) -> dict:
    """Paired candidate/control decision under the frozen TASK-14 protocol.

    ``candidate`` and ``control`` are ``{symbol: {UTC YYYY-MM-DD: net PnL}}``
    decimal-string panels. A shared calendar and a shared initial capital
    denominator are used for both endpoints; every bootstrap replicate draws one
    stationary-bootstrap index vector and applies it to candidate and control.

    Verdict rule (preregistered):
      * ``WAITING_COMPUTE_BUDGET`` when B+1 >= max(10000, ceil(20m/alpha_j))
        cannot fit inside ``max_resamples``; no result is reported as resolved;
      * ``PROMOTE`` only when the IUT/Holm rejection holds, both endpoints have
        power at least ``power_target``, and the frozen B requirement is met;
      * ``NO_PROMOTION`` only when a resolved run shows negative evidence, that
        is an endpoint's one-sided 95% upper CI lies below its floor;
      * ``INSUFFICIENT_EVIDENCE`` otherwise (nonstationary, degenerate variance,
        unresolved compute, low power, or a non-significant result).
    """
    frozen = _frozen_alpha(family_index)
    _validate_protocol(
        alpha, block, block_sensitivity, hac_lag, b, max_resamples,
        family_size, family_index, power_target, batch,
    )
    required = required_resamples(family_size, family_index)
    # Structural validation comes first: a missing mark is MISSING_DATA even
    # when the declared compute envelope could not resolve the family.
    floor_a = _decimal(economic_floor, "economic_floor")
    floor_b = _decimal(control_floor, "control_floor")
    prep = _prepare_series(candidate, control, capital)
    if required + 1 > max_resamples:
        return {
            "status": "WAITING",
            "verdict": _VERDICT_WAITING,
            "error": "WAITING_COMPUTE_BUDGET",
            "reason_codes": ["WAITING_COMPUTE_BUDGET"],
            "minimum_B": required,
            "calendar_hash": prep["calendar_hash"],
            "n_days": len(prep["calendar"]),
            "budget": _budget_view(
                b, min(b, max_resamples), max_resamples, required, block,
                block_sensitivity, seed, family_size, family_index,
            ),
            "alpha": {
                "nominal": float(alpha),
                "frozen": frozen,
                "family_size": family_size,
                "family_index": family_index,
                "schedule": "0.05 / 2^(j+1)",
            },
            "method": _DECISION_METHOD,
            "endpoints": None,
            "holm": None,
        }
    b_used = min(b, max_resamples)
    try:
        evaluated = _evaluate_all(
            prep, float(floor_a), float(floor_b),
            alpha=float(alpha), block=block, block_sensitivity=block_sensitivity,
            hac_lag=hac_lag, b=b_used, seed=seed, batch=batch,
            family_size=family_size, power_target=power_target, min_half=min_half,
        )
    except DecisionError as exc:
        if exc.code not in ("DEGENERATE_VARIANCE", "BOOTSTRAP_DEGENERATE", "INSUFFICIENT_HISTORY"):
            raise
        return {
            "status": "BLOCKED",
            "verdict": _VERDICT_INSUFFICIENT,
            "error": exc.code,
            "reason_codes": [exc.code],
            "calendar_hash": prep["calendar_hash"],
            "n_days": len(prep["calendar"]),
            "budget": _budget_view(
                b, b_used, max_resamples, required, block,
                block_sensitivity, seed, family_size, family_index,
            ),
            "alpha": {
                "nominal": float(alpha),
                "frozen": frozen,
                "family_size": family_size,
                "family_index": family_index,
                "schedule": "0.05 / 2^(j+1)",
            },
            "method": _DECISION_METHOD,
            "endpoints": None,
            "holm": None,
        }

    stats = _endpoint_payload(
        prep, floor_a, floor_b, evaluated,
        alpha=alpha, frozen=frozen, block=block,
        block_sensitivity=block_sensitivity, hac_lag=hac_lag, b=b,
        b_used=b_used, max_resamples=max_resamples, seed=seed,
        family_size=family_size, family_index=family_index,
        power_target=power_target,
    )
    if stats.get("computable") is False or stats["primary"] is None:
        stationary = stats["stationarity"]["status"]
        reason = "NONSTATIONARY" if stationary == "FAIL" else "STATIONARITY_DIAGNOSTIC_INSUFFICIENT"
        stats.update(
            {
                "status": "BLOCKED",
                "verdict": _VERDICT_INSUFFICIENT,
                "reason_codes": [reason],
                "holm": None,
                "adjusted_p": 1.0,
                "holm_threshold": float(alpha),
                "iut_p_upper": 1.0,
                "power": None,
                "endpoints": None,
            }
        )
        return stats

    endpoint_a = stats["primary"]["endpoints"]["A"]
    endpoint_b = stats["primary"]["endpoints"]["B"]
    holm = _holm_view(stats["primary"]["iut"]["p_upper"], candidate_id, family_pvalues, family_size, float(alpha))
    verdict, reasons = _resolve_verdict(
        stats["stationarity"]["status"],
        resolved=stats["budget"]["resolved"],
        rejected=holm["self_rejected"],
        power_a=endpoint_a["power"],
        power_b=endpoint_b["power"],
        power_target=power_target,
        negative_a=endpoint_a["negative_evidence"],
        negative_b=endpoint_b["negative_evidence"],
    )
    stats["status"] = "PASS" if verdict != _VERDICT_INSUFFICIENT else "BLOCKED"
    stats["verdict"] = verdict
    stats["reason_codes"] = reasons
    stats["holm"] = holm
    stats["adjusted_p"] = holm["self_adjusted_p"]
    stats["holm_threshold"] = holm["self_threshold"]
    stats["iut_p_upper"] = stats["primary"]["iut"]["p_upper"]
    stats["power"] = {
        "target": float(power_target),
        "endpoint_a": endpoint_a["power"],
        "endpoint_b": endpoint_b["power"],
        "mde_daily_endpoint_a": endpoint_a["mde_daily"],
        "mde_daily_endpoint_b": endpoint_b["mde_daily"],
    }
    return stats


def family_decision(
    candidates: Mapping[str, Mapping[str, Mapping[str, str]]],
    control,
    capital,
    economic_floor,
    control_floor,
    *,
    alpha: float = 0.025,
    block: int = 5,
    block_sensitivity: int = 10,
    hac_lag: int = 5,
    b: int = 2000,
    max_resamples: int = 1_000_000,
    seed: int = 0,
    family_index: int = 0,
    power_target: float = 0.8,
    batch: int = 64,
    min_half: int = 50,
) -> dict:
    """Holm step-down over a fixed preregistered candidate family.

    The family size is the number of declared candidates. An invalid candidate
    is retained with p=1.0; it is never dropped before Holm, so a family can
    never gain power by silently shrinking m.
    """
    if not isinstance(candidates, Mapping) or len(candidates) == 0:
        raise DecisionError("MISSING_DATA", "candidate family is empty")
    ids = sorted(str(cid) for cid in candidates)
    panels = {str(cid): candidates[cid] for cid in candidates}
    family_size = len(ids)
    frozen = _validate_protocol(
        alpha, block, block_sensitivity, hac_lag, b, max_resamples,
        family_size, family_index, power_target, batch,
    )
    required = required_resamples(family_size, family_index)
    budget = _budget_view(
        b, min(b, max_resamples), max_resamples, required, block,
        block_sensitivity, seed, family_size, family_index,
    )
    capital_fraction = _decimal(capital, "capital")
    floor_a_string = str(_decimal(economic_floor, "economic_floor"))
    floor_b_string = str(_decimal(control_floor, "control_floor"))
    method_hash = _method_hash(
        {
            "block": block,
            "block_sensitivity": block_sensitivity,
            "hac_lag": hac_lag,
            "b": min(b, max_resamples),
            "max_resamples": max_resamples,
            "seed": seed,
            "alpha": float(alpha),
            "family_size": family_size,
            "family_index": family_index,
            "power_target": float(power_target),
            "economic_floor": floor_a_string,
            "control_floor": floor_b_string,
            "capital": str(capital_fraction),
        }
    )
    alpha_block = {
        "nominal": float(alpha),
        "frozen": frozen,
        "family_size": family_size,
        "family_index": family_index,
        "schedule": "0.05 / 2^(j+1)",
    }
    if required + 1 > max_resamples:
        return {
            "status": "WAITING",
            "verdict": _VERDICT_WAITING,
            "error": "WAITING_COMPUTE_BUDGET",
            "reason_codes": ["WAITING_COMPUTE_BUDGET"],
            "family_size": family_size,
            "family_index": family_index,
            "minimum_B": required,
            "budget": budget,
            "alpha": alpha_block,
            "holm": None,
            "candidates": {
                cid: {
                    "status": "WAITING",
                    "verdict": _VERDICT_WAITING,
                    "reason_codes": ["WAITING_COMPUTE_BUDGET"],
                    "retained_in_family": True,
                    "p_iut_upper": 1.0,
                    "endpoints": None,
                }
                for cid in ids
            },
            "inputs": {
                "economic_floor": floor_a_string,
                "control_floor": floor_b_string,
                "capital": str(capital_fraction),
                "block": block,
                "sensitivity_block": block_sensitivity,
                "hac_lag": hac_lag,
                "b": b,
                "max_resamples": max_resamples,
                "seed": seed,
            },
            "method": _DECISION_METHOD,
            "method_hash": method_hash,
        }

    entries: dict[str, dict] = {}
    pvalues: list[float] = []
    for cid in ids:
        try:
            stats = paired_endpoints(
                panels[cid], control, capital, economic_floor, control_floor,
                alpha=alpha, block=block, block_sensitivity=block_sensitivity,
                hac_lag=hac_lag, b=b, max_resamples=max_resamples, seed=seed,
                family_size=family_size, family_index=family_index,
                power_target=power_target, batch=batch, min_half=min_half,
            )
        except DecisionError as exc:
            entries[cid] = {
                "status": "BLOCKED",
                "verdict": _VERDICT_INSUFFICIENT,
                "error": exc.code,
                "reason_codes": [exc.code],
                "retained_in_family": True,
                "p_iut_upper": 1.0,
                "endpoints": None,
                "sensitivity": None,
                "stationarity": None,
            }
            pvalues.append(1.0)
            continue
        computable = stats.get("computable", True) and stats["primary"] is not None
        p_self = stats["primary"]["iut"]["p_upper"] if computable else 1.0
        entries[cid] = {
            "status": "PASS",
            "retained_in_family": True,
            "p_iut_upper": p_self,
            "stats": stats,
        }
        pvalues.append(p_self)

    table = holm_adjusted(pvalues, float(alpha))
    for position, cid in enumerate(ids):
        entry = entries[cid]
        entry["adjusted_p"] = table["adjusted"][position]
        entry["holm_threshold"] = table["thresholds"][position]
        entry["holm_rejected"] = table["rejected"][position]
        if "stats" in entry:
            stats = entry.pop("stats")
            if stats.get("computable") is False or stats["primary"] is None:
                stationary = stats["stationarity"]["status"]
                entry.update(stats)
                entry["verdict"] = _VERDICT_INSUFFICIENT
                entry["reason_codes"] = [
                    "NONSTATIONARY" if stationary == "FAIL" else "STATIONARITY_DIAGNOSTIC_INSUFFICIENT",
                    "HOLM_RETAINED_WITH_P1",
                ]
                entry["status"] = "BLOCKED"
                continue
            endpoint_a = stats["primary"]["endpoints"]["A"]
            endpoint_b = stats["primary"]["endpoints"]["B"]
            verdict, reasons = _resolve_verdict(
                stats["stationarity"]["status"],
                resolved=stats["budget"]["resolved"],
                rejected=table["rejected"][position],
                power_a=endpoint_a["power"],
                power_b=endpoint_b["power"],
                power_target=power_target,
                negative_a=endpoint_a["negative_evidence"],
                negative_b=endpoint_b["negative_evidence"],
            )
            entry.update(stats)
            entry["verdict"] = verdict
            entry["reason_codes"] = reasons
            entry["status"] = "PASS" if verdict != _VERDICT_INSUFFICIENT else "BLOCKED"
        else:
            entry["reason_codes"] = entry["reason_codes"] + ["HOLM_RETAINED_WITH_P1"]

    verdicts = [entry["verdict"] for entry in entries.values()]
    if any(v == _VERDICT_PROMOTE for v in verdicts):
        family_verdict = _VERDICT_PROMOTE
    elif verdicts and all(v == _VERDICT_NO_PROMOTION for v in verdicts):
        family_verdict = _VERDICT_NO_PROMOTION
    else:
        family_verdict = _VERDICT_INSUFFICIENT
    return {
        "status": "PASS" if family_verdict != _VERDICT_INSUFFICIENT else "BLOCKED",
        "verdict": family_verdict,
        "family_size": family_size,
        "family_index": family_index,
        "minimum_B": required,
        "budget": budget,
        "alpha": alpha_block,
        "holm": {
            "members": ids,
            "pvalues": table["pvalues"],
            "adjusted": table["adjusted"],
            "thresholds": table["thresholds"],
            "rejected": table["rejected"],
            "alpha": float(alpha),
        },
        "candidates": entries,
        "inputs": {
            "economic_floor": floor_a_string,
            "control_floor": floor_b_string,
            "capital": str(capital_fraction),
            "block": block,
            "sensitivity_block": block_sensitivity,
            "hac_lag": hac_lag,
            "b": b,
            "max_resamples": max_resamples,
            "seed": seed,
        },
        "method": _DECISION_METHOD,
        "method_hash": method_hash,
    }
