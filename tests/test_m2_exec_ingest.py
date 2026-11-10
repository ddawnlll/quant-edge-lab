"""A2: execution-coverage audit tests (no network, no downloads)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from m2_exec_ingest import FOLDS, SEALED_FROM, audit, day_range, day_present


def test_frozen_fold_windows_match_protocol() -> None:
    assert FOLDS[0]["fold_id"] == "WF-D64AF439CD8D6E7F"
    assert FOLDS[0]["train_start"] == "2024-09-01"
    assert FOLDS[0]["validation_end"] == "2025-11-30"
    assert FOLDS[1]["fold_id"] == "WF-2CCA0B04C796FBFC"
    assert SEALED_FROM == "2026-03-08"


def test_fold_windows_never_touch_sealed_reserve() -> None:
    for fold in FOLDS:
        assert fold["validation_end"] < SEALED_FROM
        assert fold["train_end"] < fold["validation_start"]


def test_audit_reflects_disk_without_inventing_coverage() -> None:
    report = audit()
    assert report["schema"] == "qel.m2_execution_coverage/1.0"
    assert report["sealed_untouched"] is True
    # The audit must report exactly what is on disk: recount independently
    # through the same per-day presence predicate over the frozen windows.
    needed: set[str] = set()
    for fold in FOLDS:
        needed.update(day_range(fold["train_start"], fold["train_end"]))
        needed.update(day_range(fold["validation_start"], fold["validation_end"]))
    needed = {d for d in needed if d < SEALED_FROM}
    recount = sum(1 for d in needed if day_present(d))
    assert report["present_days"] == recount
    assert report["missing_days"] == report["total_needed_days"] - report["present_days"]
    if report["status"] == "COMPLETE":
        assert report["missing_days"] == 0
        assert report["present_days"] == report["total_needed_days"]
    else:
        assert report["present_days"] < report["total_needed_days"]


def test_day_range_is_contiguous() -> None:
    days = day_range("2024-09-01", "2024-09-07")
    assert days == [f"2024-09-0{i}" for i in range(1, 8)]


def test_day_present_fail_closed_on_missing_dir(tmp_path) -> None:
    assert day_present("2099-01-01") is False
