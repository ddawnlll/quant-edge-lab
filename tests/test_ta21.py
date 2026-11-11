"""No-lookahead, zero-division and incremental TA21 research-feature tests."""
import math

import pytest

from qel.research.ta21 import Bar, CausalTA21, DAY_NS, NEW_MOON_EPOCH_NS, lunar_bias


def b(t, o, h, l, c, v=10):
    return Bar(t * DAY_NS, o, h, l, c, v)


def test_reject_bad_bars_and_time_regressions():
    for args in [(0, 1, 2, 0, 3, 1), (0, 2, 3, 1, 2, -1), (0, math.nan, 3, 1, 2, 1)]:
        with pytest.raises(ValueError):
            b(*args)
    engine = CausalTA21()
    engine.update(b(1, 5, 6, 4, 5))
    with pytest.raises(ValueError):
        engine.update(b(1, 5, 6, 4, 5))


def test_pivot_is_visible_only_after_right_closed_bars():
    engine = CausalTA21(left=1, right=2, atr_window=2)
    series = [b(1, 5, 6, 4, 5), b(2, 7, 10, 6, 7), b(3, 7, 8, 5, 6), b(4, 6, 7, 5, 6)]
    out = [engine.update(x) for x in series]
    assert all(not s.confirmed_pivots for s in out[:3])
    highs = [p for p in out[3].confirmed_pivots if p.kind == "HIGH"]
    assert len(highs) == 1 and highs[0].formed_at_ns == 2 * DAY_NS
    assert highs[0].confirmed_at_ns == 4 * DAY_NS


def test_prefix_invariant_with_future_suffix():
    seq = [b(i, 20+i % 3, 22+i%3, 19+i%3, 21+i%3, 10+i) for i in range(1, 22)]
    a, c = CausalTA21(left=1, right=1, rsi_window=2, atr_window=2), CausalTA21(left=1, right=1, rsi_window=2, atr_window=2)
    first = [a.update(x) for x in seq[:12]]
    second = [c.update(x) for x in seq]
    assert first == second[:12]
    assert all(e.observed_at_ns == snapshot.at_ns and e.anchor_at_ns <= snapshot.at_ns
               for snapshot in second for e in snapshot.events)


def test_first_fvg_mitigation_not_on_creation_bar():
    eng = CausalTA21(left=1, right=1, atr_window=2, fvg_impulse_atr=1.2)
    # t2 is an impulse vs t2 Wilder ATR; t3 forms a gap above t1 high.
    rows = [b(1, 10, 11, 9, 10), b(2, 11, 17, 11, 17),
            b(3, 18, 19, 18, 19), b(4, 18, 19, 10.5, 18)]
    snapshots = [eng.update(x) for x in rows]
    assert any(e.family == "fvg_created" for e in snapshots[2].events)
    assert not any(e.family == "fvg_first_mitigation" for e in snapshots[2].events)
    assert any(e.family == "fvg_first_mitigation" for e in snapshots[3].events)
    assert not eng.gaps


def test_wilder_rsi_warmup_uptrend_and_flat():
    up, flat = CausalTA21(rsi_window=3), CausalTA21(rsi_window=3)
    ups = [up.update(b(i, 11+i, 12+i, 10+i, 11+i)).rsi for i in range(1, 6)]
    flats = [flat.update(b(i, 11, 12, 10, 11)).rsi for i in range(1, 6)]
    assert ups[:3] == [None, None, None]
    assert ups[3:] == [100.0, 100.0]
    assert flats[3:] == [50.0, 50.0]


def test_vwap_uses_only_current_day_and_rvol_includes_current_closed_bar():
    eng = CausalTA21(rvol_window=2)
    a = eng.update(b(1, 10, 11, 9, 10, 10))
    v = eng.update(b(2, 20, 21, 19, 20, 10))
    c = eng.update(b(3, 30, 31, 29, 30, 20))
    assert a.vwap == 10 and v.vwap == 20 and c.vwap == 30
    assert a.rvol is None and v.rvol == 1 and c.rvol == pytest.approx(4 / 3)


def test_heikin_ashi_is_synthetic_and_lunar_has_no_signal():
    eng = CausalTA21()
    a = eng.update(b(1, 10, 12, 9, 11))
    bb = eng.update(b(2, 12, 14, 11, 13))
    assert a.ha_open == 10.5 and a.ha_close == 10.5
    assert bb.ha_open == 10.5 and bb.ha_close == 12.5
    assert lunar_bias(NEW_MOON_EPOCH_NS) == 1.0
    assert not any(e.family == "lunar" for e in a.events + bb.events)


def test_candle_geometry_and_all_events_at_close():
    eng = CausalTA21()
    eng.update(b(1, 10, 10.2, 8, 9))
    candle = eng.update(b(2, 9, 12, 8.8, 11))
    assert any(e.family == "candlestick_engulfing" and e.side == 1 for e in candle.events)
    assert all(e.observed_at_ns == candle.at_ns for e in candle.events)


def test_fibonacci_uses_confirmed_low_before_high_once():
    eng = CausalTA21(left=1, right=1, atr_window=2)
    rows = [b(1, 10, 11, 9, 10), b(2, 8, 9, 7, 8),
            b(3, 10, 12, 8, 11), b(4, 13, 15, 11, 14),
            b(5, 13, 14, 12, 13), b(6, 12, 13, 11.5, 12),
            b(7, 11, 12.5, 11, 12), b(8, 11, 12.5, 11, 12)]
    out = [eng.update(x) for x in rows]
    # Pivot high at t4 only confirmed at t5. Fib 0.382 of 7->15 = 11.944.
    assert not any(e.family == "fibonacci_0382" for s in out[:5] for e in s.events)
    events = [e for s in out for e in s.events if e.family == "fibonacci_0382"]
    assert len(events) == 1 and events[0].observed_at_ns == 6 * DAY_NS


def test_causal_market_structure_bos_and_choch():
    eng = CausalTA21(left=1, right=1, atr_window=2)
    rows = [b(1, 10, 11, 9, 10), b(2, 8, 9, 5, 8),
            b(3, 11, 12, 8, 11), b(4, 9, 10, 7, 9),
            b(5, 13, 15, 10, 13), b(6, 11, 12, 8, 11),
            b(7, 13, 14, 10, 13), b(8, 14, 16, 13, 16),
            b(9, 12, 14, 6, 7)]
    shots = [eng.update(x) for x in rows]
    assert shots[6].structure == 1
    assert any(e.family == "bos" and e.side == 1 for e in shots[7].events)
    assert any(e.family == "choch" and e.side == -1 for e in shots[8].events)
