import numpy as np
import pytest

from qel.stats.core import (
    classify,
    hac_se,
    holm,
    iut,
    minimum_b,
    studentized_bootstrap_p,
    walk_forward_splits,
)


def test_f27_iut_is_intersection_union():
    assert iut(0.01, 0.04) == 0.04
    assert iut(0.2, 0.1) == 0.2


def test_f28_holm_vectors():
    assert holm([0.001, 0.01, 0.02], 0.025) == [True, True, True]
    assert holm([0.009, 0.01, 0.02], 0.025) == [False, False, False]


def test_f28_mc_budget():
    required, ok = minimum_b(8, 8, 1_000_000)
    assert required == 1_638_399
    assert ok is False


def test_f28_classification():
    assert classify(0.3, 0.2, -0.1, 0.2, 0.01) == "INSUFFICIENT_EVIDENCE"


def test_walk_forward_purge_boundaries():
    dates = [f"2026-01-{i:02d}" for i in range(1, 21)]
    splits = walk_forward_splits(dates, 5, 3, 2, 1)
    for s in splits:
        train_end = dates.index(s["train_end"])
        test_start = dates.index(s["test_start"])
        assert test_start - train_end == 3  # 2 purge days + next day


def test_hac_and_bootstrap_smoke():
    rng = np.random.default_rng(0)
    x = rng.normal(0.1, 1.0, 60)
    assert hac_se(x) > 0
    result = studentized_bootstrap_p(x, b=64, seed=1, batch=16)
    assert 0 < result.p_raw <= 1
    assert result.p_upper >= result.p_raw


def test_hac_exact_small_sample():
    assert hac_se([1.0, 2.0, 3.0], lag=0) == pytest.approx((2.0 / 3.0 / 3.0) ** 0.5)
    assert hac_se([1.0, 2.0, 3.0, 4.0], lag=0) == pytest.approx((1.25 / 4.0) ** 0.5)
    assert hac_se([1.0, 2.0, 3.0, 4.0], lag=1) == pytest.approx(0.625)
    with pytest.raises(ValueError):
        hac_se([2.0, 2.0, 2.0])
    with pytest.raises(ValueError):
        hac_se([1.0, 2.0])


def test_walk_forward_rejects_invalid_and_short_windows():
    with pytest.raises(ValueError):
        walk_forward_splits(["a", "b"], 0, 1, 0, 0)
    with pytest.raises(ValueError):
        walk_forward_splits(["a"], 1, 1, 0, 0)


def test_null_generators_and_regime_block():
    from qel.stats.core import null_generator, stationarity_screen

    for name in ("ar1", "t5", "common_factor", "regime_shift"):
        series = null_generator(name, 80, 3)
        assert len(series) == 80
        assert np.isfinite(series).all()
    regime = null_generator("regime_shift", 80, 3)
    assert not stationarity_screen(regime)


def test_f28_required_resamples_matches_minimum_b():
    from qel.stats.core import required_resamples

    required, ok = minimum_b(8, 8, 1_000_000)
    assert required_resamples(8, 8) == required == 1_638_399
    assert ok is False
