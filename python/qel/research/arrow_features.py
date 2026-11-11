"""Shared causal vectorized features over Arrow batches (PERF v1.2 P3).

Screening-only: these features are deterministic, PIT-safe and computed
once over immutable inputs. They never replace Nautilus certified
execution; each strategy still keeps independent account/order/position
state downstream.

Semantics mirror ``qel.research.control._ema`` (alpha=2/(20+1), state
rounded to a 1e-9 grid each step, causal: value i uses inputs <= i) but
are computed with exact integer arithmetic carried across RecordBatch
boundaries, and with numpy rolling aggregates where equivalent.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

import numpy as np
import pyarrow as pa

GRID = 1_000_000_000  # 1e-9 state grid


def _round_half_even_div(numer: int, denom: int) -> int:
    """Round numer/denom to nearest integer, half-even, sign-symmetric."""
    sign = -1 if numer < 0 else 1
    mag = abs(numer)
    q, r = divmod(mag, denom)
    half = denom // 2
    # denom is odd (21) here, so exact half cannot occur; handle even anyway.
    if r > denom - r or (2 * r == denom and q % 2 == 1):
        q += 1
    return sign * q


@dataclass
class EmaState:
    """Causal streaming EMA state (units of 1e-9)."""

    window: int = 20
    state_units: int | None = None

    @property
    def alpha_num(self) -> int:
        return 2

    @property
    def denom(self) -> int:
        return self.window + 1

    def update(self, mid_units_1e9: int) -> int:
        if self.state_units is None:
            self.state_units = mid_units_1e9
        else:
            numer = self.alpha_num * mid_units_1e9 + (self.window - 1) * self.state_units
            self.state_units = _round_half_even_div(numer, self.denom)
        return self.state_units


def ema_streaming(mids_coeff_1e8: list[int], window: int = 20) -> list[int]:
    """Causal EMA in 1e-9 units. mid coeff at 1e8 -> *10 for 1e-9 units."""
    st = EmaState(window=window)
    return [st.update(int(c) * 10) for c in mids_coeff_1e8]


def ema_from_fraction_mids(mids: list[Fraction], window: int = 20) -> list[Fraction]:
    """Reference using control._ema for parity tests."""
    from .control import _ema

    return _ema(mids, window)


def rolling_mean_var(values: np.ndarray, window: int) -> tuple[np.ndarray, np.ndarray]:
    """Causal rolling mean/variance (population, partial windows at start).

    Value i uses values[max(0,i-window+1)..i] only (no future leakage).
    """
    values = np.asarray(values, dtype=np.float64)
    n = len(values)
    mean = np.empty(n, dtype=np.float64)
    var = np.empty(n, dtype=np.float64)
    for i in range(n):
        lo = max(0, i - window + 1)
        seg = values[lo : i + 1]
        mean[i] = seg.mean()
        var[i] = seg.var()
    return mean, var


def mids_from_batch(batch: pa.RecordBatch) -> list[int]:
    """Mid coeff at 1e8 from a schema-v2 batch's Quote rows: (bid+ask)/2.

    Exact for even sums; odd sums floor (control Fractions stay exact).
    Use mids_units_1e9_from_batch for exact half-step parity.
    """
    kinds = batch.column(batch.schema.get_field_index("event_type")).to_pylist()
    bids = batch.column(batch.schema.get_field_index("bid_i")).to_pylist()
    asks = batch.column(batch.schema.get_field_index("ask_i")).to_pylist()
    return [(b + a) // 2 for k, b, a in zip(kinds, bids, asks) if k == "Quote"]


def mids_units_1e9_from_batch(batch: pa.RecordBatch) -> list[int]:
    """Exact mid in 1e-9 units: (bid+ask)*5, no truncation."""
    kinds = batch.column(batch.schema.get_field_index("event_type")).to_pylist()
    bids = batch.column(batch.schema.get_field_index("bid_i")).to_pylist()
    asks = batch.column(batch.schema.get_field_index("ask_i")).to_pylist()
    return [(b + a) * 5 for k, b, a in zip(kinds, bids, asks) if k == "Quote"]


def ema_over_batches(batches: list[pa.RecordBatch], window: int = 20) -> list[int]:
    """EMA over Quote mids with state carried across batch boundaries."""
    st = EmaState(window=window)
    out: list[int] = []
    for batch in batches:
        for units in mids_units_1e9_from_batch(batch):
            out.append(st.update(units))
    return out


def signal_above_below(mids_coeff_1e8: list[int], ema_units_1e9: list[int]) -> list[str]:
    """Per-bar relation of mid vs EMA (control uses > / < on exact values)."""
    out = []
    for c, e in zip(mids_coeff_1e8, ema_units_1e9):
        mid_units = c * 10
        out.append("ABOVE" if mid_units > e else ("BELOW" if mid_units < e else "EQUAL"))
    return out
