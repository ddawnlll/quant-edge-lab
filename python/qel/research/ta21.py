"""Incremental, causal TA research features (no orders, fitting, or OOS access).

Every output is observable only at a closed bar's close_time_ns; consumers
must separately enforce market-delivery latency and next-executable-event fills.
This research-only Python reference does not alter the frozen Rust strategy DAG.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from math import cos, isfinite, pi

DAY_NS = 86_400_000_000_000
NEW_MOON_EPOCH_NS = 947182440_000_000_000  # 2000-01-06 18:14 UTC
SYNODIC_DAYS = 29.530588853


@dataclass(frozen=True)
class Bar:
    close_time_ns: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        if type(self.close_time_ns) is not int or self.close_time_ns < 0:
            raise ValueError("close_time_ns must be a nonnegative UTC ns integer")
        if not all(isfinite(x) for x in (self.open, self.high, self.low, self.close, self.volume)):
            raise ValueError("OHLCV must be finite")
        if min(self.open, self.high, self.low, self.close) <= 0 or self.volume < 0:
            raise ValueError("positive price and nonnegative volume required")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("inconsistent OHLC bar")


@dataclass(frozen=True)
class Pivot:
    kind: str
    price: float
    formed_at_ns: int
    confirmed_at_ns: int


@dataclass(frozen=True)
class Event:
    family: str
    side: int  # +1 long, -1 short, 0 informational
    observed_at_ns: int
    anchor_at_ns: int
    detail: str


@dataclass(frozen=True)
class Snapshot:
    at_ns: int
    atr: float | None
    rsi: float | None
    vwap: float | None
    rvol: float | None
    ha_open: float
    ha_close: float
    lunar_bias: float
    structure: int  # +1 HH/HL, -1 LH/LL, 0 unknown/transition
    confirmed_pivots: tuple[Pivot, ...]
    events: tuple[Event, ...]


def lunar_bias(close_time_ns: int) -> float:
    """Approximate UTC lunar phase feature, not evidence of a tradeable edge."""
    phase = (((close_time_ns - NEW_MOON_EPOCH_NS) / DAY_NS) % SYNODIC_DAYS) / SYNODIC_DAYS
    return cos(2 * pi * phase)


class CausalTA21:
    """Bounded-memory incremental reference for initial TA21 framework families.

    Implemented: causal pivots, Fibonacci 0.382 retest, 3-bar FVG and first
    mitigation, candle geometries, Heikin Ashi, RSI, UTC-day VWAP, RVOL,
    market structure, BOS/CHoCH and lunar phase. Not a strategy or fill model.
    """

    def __init__(
        self, *, left: int = 2, right: int = 2, atr_window: int = 14,
        rsi_window: int = 14, rvol_window: int = 20, fvg_impulse_atr: float = 1.2,
        fib_atr_tolerance: float = 0.1, fvg_ttl_bars: int = 128,
    ) -> None:
        if left < 1 or right < 1 or any(w < 1 for w in (atr_window, rsi_window, rvol_window)):
            raise ValueError("invalid warmup or causal pivot windows")
        if not (0 < fvg_impulse_atr and 0 <= fib_atr_tolerance and fvg_ttl_bars >= 1):
            raise ValueError("invalid tolerance or FVG TTL")
        self.left, self.right = left, right
        self.atr_window, self.rsi_window, self.rvol_window = atr_window, rsi_window, rvol_window
        self.fvg_impulse_atr, self.fib_atr_tolerance = fvg_impulse_atr, fib_atr_tolerance
        self.fvg_ttl_bars = fvg_ttl_bars
        self.bars: deque[Bar] = deque(maxlen=max(left + right + 1, 3))
        self.pivots: deque[Pivot] = deque(maxlen=128)
        self.volumes: deque[float] = deque(maxlen=rvol_window)
        self.gaps: list[tuple[int, int, int, float, float]] = []  # birth_idx, ts, side, low, high
        self.last_ns: int | None = None
        self.prev_close: float | None = None
        self.prev_bar: Bar | None = None
        self.prev_atr: float | None = None
        self.tr_seed = 0.0
        self.tr_count = 0
        self.atr: float | None = None
        self.rsi_count = 0
        self.gain_seed = self.loss_seed = 0.0
        self.avg_gain: float | None = None
        self.avg_loss: float | None = None
        self.ha_open: float | None = None
        self.ha_close: float | None = None
        self.day: int | None = None
        self.vwap_num = self.vwap_den = 0.0
        self.idx = 0
        self.fib_fired: set[int] = set()

    def _atr_step(self, bar: Bar) -> float | None:
        tr = bar.high - bar.low if self.prev_close is None else max(
            bar.high - bar.low, abs(bar.high - self.prev_close), abs(bar.low - self.prev_close)
        )
        self.tr_count += 1
        if self.tr_count <= self.atr_window:
            self.tr_seed += tr
            if self.tr_count == self.atr_window:
                self.atr = self.tr_seed / self.atr_window
        else:
            assert self.atr is not None
            self.atr = (self.atr * (self.atr_window - 1) + tr) / self.atr_window
        return self.atr

    def _rsi_step(self, close: float) -> float | None:
        if self.prev_close is None:
            return None
        d = close - self.prev_close
        gain, loss = max(d, 0.0), max(-d, 0.0)
        self.rsi_count += 1
        if self.rsi_count <= self.rsi_window:
            self.gain_seed += gain
            self.loss_seed += loss
            if self.rsi_count == self.rsi_window:
                self.avg_gain = self.gain_seed / self.rsi_window
                self.avg_loss = self.loss_seed / self.rsi_window
        else:
            assert self.avg_gain is not None and self.avg_loss is not None
            self.avg_gain = (self.avg_gain * (self.rsi_window - 1) + gain) / self.rsi_window
            self.avg_loss = (self.avg_loss * (self.rsi_window - 1) + loss) / self.rsi_window
        if self.avg_gain is None or self.avg_loss is None:
            return None
        if self.avg_loss == 0:
            return 100.0 if self.avg_gain > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + self.avg_gain / self.avg_loss)

    def _structure(self) -> int:
        highs = [p.price for p in self.pivots if p.kind == "HIGH"]
        lows = [p.price for p in self.pivots if p.kind == "LOW"]
        if len(highs) < 2 or len(lows) < 2:
            return 0
        if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
            return 1
        if highs[-1] < highs[-2] and lows[-1] < lows[-2]:
            return -1
        return 0

    def _confirm_pivots(self, bar: Bar) -> list[Pivot]:
        n = self.left + self.right + 1
        if len(self.bars) != n:
            return []
        xs = list(self.bars)
        c = xs[self.left]
        got: list[Pivot] = []
        # Unique extrema suppress ambiguous same-level tied pivots.
        for kind, value, vals in (
            ("HIGH", c.high, [b.high for b in xs]),
            ("LOW", c.low, [b.low for b in xs]),
        ):
            if vals.count(value) == 1 and value == (max(vals) if kind == "HIGH" else min(vals)):
                p = Pivot(kind, value, c.close_time_ns, bar.close_time_ns)
                self.pivots.append(p)
                got.append(p)
        return got

    def update(self, bar: Bar) -> Snapshot:
        if self.last_ns is not None and bar.close_time_ns <= self.last_ns:
            raise ValueError("duplicate/out-of-order close time")
        # Previous confirmed structure and pivots only: current bar's newly
        # confirmed pivots are not used to retroactively classify its crossing.
        old_structure = self._structure()
        old_highs = [p for p in self.pivots if p.kind == "HIGH"]
        old_lows = [p for p in self.pivots if p.kind == "LOW"]
        old_close = self.prev_close
        old_bar = self.prev_bar
        atr = self._atr_step(bar)
        rsi = self._rsi_step(bar.close)
        events: list[Event] = []

        def signal(family: str, side: int, anchor: int, detail: str) -> None:
            events.append(Event(family, side, bar.close_time_ns, anchor, detail))

        if old_bar is not None:
            body, prev_body = bar.close - bar.open, old_bar.close - old_bar.open
            if prev_body < 0 < body and bar.close > old_bar.open and bar.open <= old_bar.close and body > abs(prev_body):
                signal("candlestick_engulfing", 1, old_bar.close_time_ns, "bullish")
            if prev_body > 0 > body and bar.close < old_bar.open and bar.open >= old_bar.close and abs(body) > prev_body:
                signal("candlestick_engulfing", -1, old_bar.close_time_ns, "bearish")
        spread = bar.high - bar.low
        if spread > 0:
            body = abs(bar.close - bar.open)
            upper = bar.high - max(bar.open, bar.close)
            lower = min(bar.open, bar.close) - bar.low
            if body / spread <= 0.05:
                signal("candlestick_doji", 0, bar.close_time_ns, "indecision")
            if lower >= 2 * body and upper / spread <= 0.15 and body / spread >= 0.10:
                signal("candlestick_hammer", 1, bar.close_time_ns, "rejection")
            if upper >= 2 * body and lower / spread <= 0.15 and body / spread >= 0.10:
                signal("candlestick_shooting_star", -1, bar.close_time_ns, "rejection")

        # Mitigation may only happen on bars after the three-bar FVG completes.
        survivors = []
        for born, ts, side, lo, hi in self.gaps:
            if self.idx - born > self.fvg_ttl_bars:
                continue
            touched = bar.low <= hi and bar.high >= lo
            if touched:
                if (side == 1 and bar.close >= lo) or (side == -1 and bar.close <= hi):
                    signal("fvg_first_mitigation", side, ts, "first touch; close confirms")
            else:
                survivors.append((born, ts, side, lo, hi))
        self.gaps = survivors
        if len(self.bars) >= 2 and self.prev_atr is not None and self.prev_atr > 0:
            first, middle = list(self.bars)[-2:]
            if abs(middle.close - middle.open) > self.fvg_impulse_atr * self.prev_atr:
                if bar.low > first.high:
                    self.gaps.append((self.idx, bar.close_time_ns, 1, first.high, bar.low))
                    signal("fvg_created", 1, bar.close_time_ns, "research zone; not an entry")
                elif bar.high < first.low:
                    self.gaps.append((self.idx, bar.close_time_ns, -1, bar.high, first.low))
                    signal("fvg_created", -1, bar.close_time_ns, "research zone; not an entry")
        # Bound zones without changing semantics of recent, unmitigated gaps.
        self.gaps = self.gaps[-128:]

        if old_close is not None and old_structure != 0:
            high = old_highs[-1] if old_highs else None
            low = old_lows[-1] if old_lows else None
            if old_structure == 1:
                if high and old_close <= high.price < bar.close:
                    signal("bos", 1, high.confirmed_at_ns, "HH/HL continuation")
                if low and old_close >= low.price > bar.close:
                    signal("choch", -1, low.confirmed_at_ns, "HH/HL broken")
            else:
                if low and old_close >= low.price > bar.close:
                    signal("bos", -1, low.confirmed_at_ns, "LH/LL continuation")
                if high and old_close <= high.price < bar.close:
                    signal("choch", 1, high.confirmed_at_ns, "LH/LL broken")

        if atr is not None and atr > 0 and old_highs and old_lows:
            high = old_highs[-1]
            low = next((p for p in reversed(old_lows) if p.formed_at_ns < high.formed_at_ns), None)
            if low and high.price > low.price and high.formed_at_ns not in self.fib_fired:
                level = high.price - 0.382 * (high.price - low.price)
                if bar.low <= level + self.fib_atr_tolerance * atr and bar.close >= level and bar.low > low.price:
                    signal("fibonacci_0382", 1, high.confirmed_at_ns, "closed-bar upward impulse pullback")
                    self.fib_fired.add(high.formed_at_ns)

        day = bar.close_time_ns // DAY_NS
        if day != self.day:
            self.day = day
            self.vwap_num = self.vwap_den = 0.0
        self.vwap_num += ((bar.high + bar.low + bar.close) / 3.0) * bar.volume
        self.vwap_den += bar.volume
        vwap = self.vwap_num / self.vwap_den if self.vwap_den > 0 else None
        self.volumes.append(bar.volume)
        vbase = sum(self.volumes) / self.rvol_window if len(self.volumes) == self.rvol_window else 0.0
        rvol = bar.volume / vbase if vbase > 0 else None
        ha_close = (bar.open + bar.high + bar.low + bar.close) / 4.0
        ha_open = (bar.open + bar.close) / 2.0 if self.ha_open is None else (self.ha_open + self.ha_close) / 2.0
        self.ha_open, self.ha_close = ha_open, ha_close

        self.bars.append(bar)
        confirmed = self._confirm_pivots(bar)
        self.prev_atr = atr
        self.prev_close = bar.close
        self.prev_bar = bar
        self.last_ns = bar.close_time_ns
        self.idx += 1
        # Keep only identifiers for retained pivots; memory remains bounded.
        live = {p.formed_at_ns for p in self.pivots if p.kind == "HIGH"}
        self.fib_fired.intersection_update(live)
        return Snapshot(bar.close_time_ns, atr, rsi, vwap, rvol, ha_open, ha_close,
                        lunar_bias(bar.close_time_ns), self._structure(), tuple(confirmed), tuple(events))
