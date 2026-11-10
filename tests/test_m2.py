"""M2 development walk-forward runner tests.

These tests exercise split construction, frozen-manifest refusal, the
capital-relative notional rule and the 20 USDT venue feasibility result.  The
heavy candidate/control run is recorded in evidence and is not re-timed here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qel.research.m2 import (
    DEV_WEEK_END,
    DEV_WEEK_START,
    _safe_for_jcs,
    check_frozen_manifest,
    load_execution_days,
    make_development_fold,
    run_m2,
)

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "native"
BINARY = Path("/home/quant/quant/.build-cache/qel-native/release/qel-native-runner")
MANIFEST = ROOT / "evidence" / "labd" / "PROTOCOL_MONTHLY_26M.json"
STRATEGY = ROOT / "policy" / "examples" / "taker_1m.json"


def _dev_dates() -> list[str]:
    from datetime import date, timedelta

    start = date.fromisoformat(DEV_WEEK_START)
    return [(start + timedelta(days=i)).isoformat() for i in range(7)]


def test_development_fold_is_three_train_one_purge_three_validation() -> None:
    fold = make_development_fold(_dev_dates())
    assert fold["train_dates"] == ["2024-09-01", "2024-09-02", "2024-09-03"]
    assert fold["purge_dates"] == ["2024-09-04"]
    assert fold["validation_dates"] == [
        "2024-09-05",
        "2024-09-06",
        "2024-09-07",
    ]
    assert fold["train_end"] < fold["validation_start"]
    assert fold["purge_days"] == 1
    assert fold["scope"] == "DEVELOPMENT_WEEK_SCOPE"


def test_development_fold_refuses_short_history() -> None:
    with pytest.raises(Exception) as exc:
        make_development_fold(_dev_dates()[:5])
    assert "INSUFFICIENT_DEVELOPMENT_DAYS" in str(exc.value)


def test_frozen_manifest_marks_missing_execution_days() -> None:
    manifest = json.loads(MANIFEST.read_text())
    execution = load_execution_days(str(DATA), _dev_dates())
    assert len(execution) == 7
    check = check_frozen_manifest(manifest, execution)
    assert check["fold_count"] >= 2
    assert check["status"] == "INSUFFICIENT_EXECUTION_DATA"
    assert all(not fold["executable"] for fold in check["folds"])
    assert any(fold["missing_execution_days"] for fold in check["folds"])


def test_notional_is_capital_relative_and_20_usdt_is_infeasible() -> None:
    if not (DATA.is_dir() and BINARY.exists() and MANIFEST.exists()):
        pytest.skip("development data or native runner not present")
    report = run_m2(
        manifest_path=str(MANIFEST),
        data_root=str(DATA),
        binary=str(BINARY),
        strategy_base_path=str(STRATEGY),
        out_dir="/tmp/qel-m2-test-20",
        capital="200",
        risk_fraction="0.1",
        control_replications=1,
        family_size=2,
        workers=1,
    )
    assert report["capital"] == "200"
    assert report["risk_fraction"] == "0.1"
    assert report["requested_notional"] == "20"
    assert report["status"] == "INFEASIBLE_VENUE_MIN_NOTIONAL"
    assert report["venue"]["feasible"] is False
    vs = report["venue"]
    # 20 USDT is less than the venue minimum: it must be reported, not rounded up.
    from fractions import Fraction

    assert Fraction(report["requested_notional"]) < Fraction(vs["min_notional"])
    assert Fraction(vs["min_notional"]) == Fraction(vs["min_quantity"]) * Fraction(
        vs["reference_price"]
    )
    assert report["manifest_check"]["status"] == "INSUFFICIENT_EXECUTION_DATA"


def test_large_report_integers_are_stringified_for_jcs() -> None:
    from qel.contract.canonical import hash_jcs

    value = {"exposure_notional_ns": 1802728080000000000, "small": 7}
    safe = _safe_for_jcs(value)
    assert safe["exposure_notional_ns"] == "1802728080000000000"
    assert safe["small"] == 7
    assert len(hash_jcs(safe)) == 64
