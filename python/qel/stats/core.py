"""Dependent statistics: HAC, stationary bootstrap, IUT, Holm and calibration."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np

try:
    from scipy.stats import beta as _beta
except Exception:  # pragma: no cover
    _beta = None


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
    while done < b:
        chunk = min(batch, b - done)
        for _ in range(chunk):
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
        done += chunk
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


def null_pipeline(series: np.ndarray, m: int = 4, controls: int = 2, alpha: float = 0.025, b: int = 200, seed: int = 0) -> bool:
    """One full null program: screen m candidates + controls, IUT, Holm."""
    rng = np.random.default_rng(seed)
    pvals = []
    for i in range(m):
        candidate = series + rng.normal(0, 0.05, size=len(series))
        control = rng.normal(0, 1, size=len(series))
        try:
            p_c = studentized_bootstrap_p(candidate, b=b, seed=seed + i, batch=32).p_upper
            p_ctrl = studentized_bootstrap_p(control, b=b, seed=seed + 100 + i, batch=32).p_upper
        except ValueError:
            return False
        pvals.append(iut(p_c, p_ctrl))
    rejected = holm(pvals, alpha)
    return any(rejected)


def _null_program(args: tuple[int, int, int, int, int]) -> bool:
    i, n, m, b, seed = args
    phi = 0.2 + 0.6 * ((i % 5) / 4.0)
    series = ar1_series(n, phi, 1.0, seed + i)
    return null_pipeline(series, m=m, b=b, seed=seed + i)


def calibrate_nulls(
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
    tasks = [(i, n, m, b, seed) for i in range(programs)]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        promoted = sum(1 for result in pool.map(_null_program, tasks) if result)
    rate = promoted / programs
    # 95% exact Clopper-Pearson upper bound.
    if _beta is not None:
        upper = float(_beta.ppf(0.95, promoted + 1, max(programs - promoted, 1))) if promoted < programs else 1.0
    else:  # conservative fallback
        upper = min(1.0, (promoted + 1) / max(programs, 1) + 0.05)
    return {
        "programs": programs,
        "promotions": promoted,
        "false_promotion_rate": rate,
        "binomial_upper_95": upper,
        "threshold": 0.075,
        "pass": upper <= 0.075,
        "note": "bounded local calibration; not the full B6 1000-program evidence",
    }
