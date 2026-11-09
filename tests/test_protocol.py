"""TASK-12 protocol: calendar, sealed reserve, folds, purge and preregistration."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from qel.research import protocol
from qel.research.protocol import (
    DAY_NS,
    FoldScopedCache,
    ProtocolError,
    add_months,
    build_calendar,
    epoch_ns,
    preregistration_manifest,
    protocol_report,
    purge_training_samples,
    read_reserved_payload,
    research_dates,
    reserve_days,
    scoped_fit,
    split_manifest,
)
from qel.research.screen import control_spec, split_manifest as screen_split_manifest


def _days(start: str, count: int) -> list[str]:
    first = date.fromisoformat(start)
    return [(first + timedelta(days=i)).isoformat() for i in range(count)]


def _two_symbol_payload(start: str, count: int) -> dict:
    dates = _days(start, count)
    return {"symbols": {"BTC": dates, "ETH": dates}}


def _calendar_26m() -> dict:
    return build_calendar(_two_symbol_payload("2024-01-01", 791))


def test_20_day_calendar_rejected_with_insufficient_history():
    payload = {"symbol": "BTC", "dates": _days("2024-01-01", 20)}
    calendar = build_calendar(payload)
    assert calendar["reserved"]["day_count"] == 6
    assert calendar["discovery"]["day_count"] == 14
    with pytest.raises(ProtocolError) as exc:
        split_manifest(calendar)
    assert exc.value.code == "INSUFFICIENT_HISTORY"
    detail = exc.value.detail
    assert detail["folds_possible"] == 0
    assert detail["shortfall_days"] == detail["required_discovery_days"] - detail["available_discovery_days"]
    report = protocol_report(payload)
    assert report["status"] == "INSUFFICIENT_HISTORY"


def test_exact_calendar_month_boundaries_12_3_3():
    calendar = _calendar_26m()
    split = split_manifest(calendar)
    assert split["protocol"]["train_months"] == 12
    assert split["protocol"]["validation_months"] == 3
    assert split["protocol"]["stride_months"] == 3
    first, second = split["folds"]
    assert first["train_start"] == "2024-01-01"
    assert first["train_end"] == "2024-12-31"
    assert first["validation_start"] == "2025-01-01"
    assert first["validation_end"] == "2025-03-31"
    assert second["train_start"] == "2024-01-01"
    assert second["train_end"] == "2025-03-31"
    assert second["validation_start"] == "2025-04-01"
    assert second["validation_end"] == "2025-06-30"
    assert second["train_months"] == 15
    assert second["expanding"] is True


def test_final_reserve_is_ceil_30_percent_sealed_and_excluded():
    for count in (20, 100, 791):
        calendar = build_calendar({"symbol": "BTC", "dates": _days("2024-01-01", count)})
        expected = (3 * count + 9) // 10
        assert reserve_days(count) == expected
        assert calendar["reserved"]["day_count"] == expected
        assert calendar["reserved"]["sealed"] is True
        assert calendar["reserved"]["readable"] is False
        assert calendar["reserved"]["payload_custody"] == "HASH_ONLY_UNFETCHED"
        assert calendar["reserved"]["dates"] == calendar["dates"][count - expected :]
        assert not set(calendar["reserved"]["dates"]) & set(calendar["discovery"]["dates"])
        for entry in calendar["reserved"]["hashes"].values():
            assert "payload" not in entry

    calendar = _calendar_26m()
    split = split_manifest(calendar)
    reserved = set(calendar["reserved"]["dates"])
    for fold in split["folds"]:
        assert fold["validation_end"] < calendar["reserved"]["dates"][0]
        assert not reserved & set(research_dates(calendar))
    assert research_dates(calendar) == calendar["discovery"]["dates"]
    with pytest.raises(ProtocolError) as exc:
        read_reserved_payload(calendar, calendar["reserved"]["dates"][0])
    assert exc.value.code == "SEALED_RESERVE_ACCESS"


def test_purge_at_exact_validation_boundary():
    validation_start = epoch_ns("2025-01-01")
    samples = [
        {"sample_id": "before_1ns", "label_end_ns": str(validation_start - 1)},
        {"sample_id": "at", "label_end_ns": str(validation_start)},
        {"sample_id": "after_1ns", "label_end_ns": str(validation_start + 1)},
        {"sample_id": "before_1day", "label_end_ns": str(validation_start - DAY_NS)},
        {"sample_id": "after_1day", "label_end_ns": str(validation_start + DAY_NS)},
    ]
    result = purge_training_samples(samples, validation_start, DAY_NS)
    kept = {row["sample_id"] for row in result["kept"]}
    purged = {row["sample"]["sample_id"]: row["code"] for row in result["purged"]}
    assert kept == {"before_1ns", "before_1day"}
    assert purged == {"at": "LABEL_OVERLAP", "after_1ns": "LABEL_OVERLAP", "after_1day": "LABEL_OVERLAP"}

    horizon_result = purge_training_samples(
        [{"decision_date": "2024-12-31"}, {"decision_date": "2024-12-30"}],
        validation_start,
        DAY_NS,
    )
    assert [row["sample"]["decision_date"] for row in horizon_result["purged"]] == ["2024-12-31"]
    assert [row["decision_date"] for row in horizon_result["kept"]] == ["2024-12-30"]


def test_embargo_at_equal_time_boundary():
    validation_start = epoch_ns("2025-01-01")
    cutoff = validation_start - DAY_NS
    samples = [
        {"sample_id": "at_cutoff", "label_end_ns": str(cutoff)},
        {"sample_id": "after_cutoff_1ns", "label_end_ns": str(cutoff + 1)},
        {"sample_id": "before_cutoff_1ns", "label_end_ns": str(cutoff - 1)},
        {"sample_id": "validation_start", "label_end_ns": str(validation_start)},
    ]
    result = purge_training_samples(samples, validation_start, DAY_NS, embargo_ns=DAY_NS)
    kept = {row["sample_id"] for row in result["kept"]}
    purged = {row["sample"]["sample_id"]: row["code"] for row in result["purged"]}
    assert kept == {"at_cutoff", "before_cutoff_1ns"}
    assert purged == {"after_cutoff_1ns": "EMBARGO", "validation_start": "LABEL_OVERLAP"}
    assert result["effective_label_cutoff_ns"] == str(cutoff)

    latency_only = purge_training_samples(
        [{"sample_id": "at_cutoff", "label_end_ns": str(cutoff)}],
        validation_start,
        DAY_NS,
        latency_ns=DAY_NS,
    )
    assert latency_only["kept_count"] == 1


def test_cross_symbol_and_validation_leakage_rejected():
    calendar = _calendar_26m()
    fold = split_manifest(calendar)["folds"][0]
    train_day = fold["train_end"]
    assert scoped_fit(fold, "BTC", [{"symbol": "BTC", "date": train_day}], calendar=calendar)["status"] == "PASS"
    with pytest.raises(ProtocolError) as exc:
        scoped_fit(fold, "BTC", [{"symbol": "ETH", "date": train_day}], calendar=calendar)
    assert exc.value.code == "CROSS_SYMBOL_LEAKAGE"
    for foreign_day in (fold["validation_start"], calendar["reserved"]["dates"][0]):
        with pytest.raises(ProtocolError) as exc:
            scoped_fit(fold, "BTC", [{"symbol": "ETH", "date": foreign_day}], calendar=calendar)
        assert exc.value.code == "CROSS_SYMBOL_LEAKAGE"
    with pytest.raises(ProtocolError) as exc:
        scoped_fit(fold, "BTC", [{"symbol": "BTC", "date": fold["validation_start"]}], calendar=calendar)
    assert exc.value.code == "FIT_SCOPE_VIOLATION"
    with pytest.raises(ProtocolError) as exc:
        scoped_fit(fold, "BTC", [{"symbol": "BTC", "date": calendar["reserved"]["dates"][0]}], calendar=calendar)
    assert exc.value.code == "SEALED_RESERVE_ACCESS"


def test_whole_dataset_fit_and_cache_rejected():
    calendar = _calendar_26m()
    fold = split_manifest(calendar)["folds"][0]
    whole_fold = dict(fold)
    whole_fold["train_start"] = calendar["first_date"]
    whole_fold["train_end"] = calendar["last_date"]
    with pytest.raises(ProtocolError) as exc:
        scoped_fit(whole_fold, "BTC", [{"symbol": "BTC", "date": calendar["last_date"]}], calendar=calendar)
    assert exc.value.code == "WHOLE_DATASET_FIT"
    with pytest.raises(ProtocolError) as exc:
        scoped_fit(fold, None, [], calendar=calendar)
    assert exc.value.code == "WHOLE_DATASET_FIT"

    cache = FoldScopedCache()
    cache.put(fold, "BTC", {"fit": "BTC"})
    assert cache.get(fold, "BTC") == {"fit": "BTC"}
    with pytest.raises(ProtocolError) as exc:
        cache.get(fold, "ALL")
    assert exc.value.code == "WHOLE_DATASET_CACHE"
    with pytest.raises(ProtocolError) as exc:
        cache.get_global()
    assert exc.value.code == "WHOLE_DATASET_CACHE"


def test_manifest_and_split_are_deterministic_across_reruns():
    first = protocol_report(_two_symbol_payload("2024-01-01", 791))
    second = protocol_report(_two_symbol_payload("2024-01-01", 791))
    assert first["status"] == second["status"] == "PASS"
    assert first["calendar"]["calendar_hash"] == second["calendar"]["calendar_hash"]
    assert first["split_manifest"]["split_hash"] == second["split_manifest"]["split_hash"]
    assert first["split_manifest"]["split_id"] == second["split_manifest"]["split_id"]
    assert first["preregistration"]["manifest_hash"] == second["preregistration"]["manifest_hash"]
    assert first["preregistration"]["manifest_id"] == second["preregistration"]["manifest_id"]

    calendar = _calendar_26m()
    split = split_manifest(calendar)
    again = preregistration_manifest(calendar, split_manifest(build_calendar(_two_symbol_payload("2024-01-01", 791))))
    assert again["manifest_hash"] == first["preregistration"]["manifest_hash"]
    assert split["split_hash"] == first["split_manifest"]["split_hash"]


def test_preregistration_freezes_rules_and_12_variant_reservation():
    report = protocol_report(_two_symbol_payload("2024-01-01", 791))
    manifest = report["preregistration"]
    assert manifest["frozen"] is True
    assert manifest["frozen_before_execution"] is True
    assert manifest["execution_started"] is False
    assert manifest["family"]["family_size"] == 12
    assert manifest["trial_reservation"]["reserved_trials_total"] == 12
    assert len(manifest["trial_reservation"]["per_variant"]) == 12
    assert {row["reserved_trials"] for row in manifest["trial_reservation"]["per_variant"]} == {1}
    assert manifest["trial_reservation"]["append_only"] is True
    assert manifest["split"]["split_hash"] == report["split_manifest"]["split_hash"]
    assert manifest["split"]["cutoff_hash"]
    assert set(manifest["input_hashes"]) >= {
        "calendar_hash",
        "discovery_dates_hash",
        "reserved_manifest_hash",
        "reserved_dates_hash",
    }
    for key in ("hypothesis", "economic_thresholds", "selection_rules", "costs", "fidelity"):
        assert manifest[key]
    assert report["scope_checks"]["status"] == "PASS"
    assert report["scope_checks"]["failed_count"] == 0


def test_incomplete_duplicate_misaligned_and_gap_rejected():
    with pytest.raises(ProtocolError) as exc:
        build_calendar([{"symbol": "BTC", "date": "2024-01-01", "complete": False}])
    assert exc.value.code == "INCOMPLETE_DAY"
    with pytest.raises(ProtocolError) as exc:
        build_calendar(["2024-01-01", "2024-01-01"])
    assert exc.value.code == "DUPLICATE_DATE"
    with pytest.raises(ProtocolError) as exc:
        build_calendar({"symbols": {"BTC": ["2024-01-01"], "ETH": ["2024-01-02"]}})
    assert exc.value.code == "MISALIGNED_CALENDAR"
    with pytest.raises(ProtocolError) as exc:
        build_calendar(["2024-01-01", "2024-01-03"])
    assert exc.value.code == "CALENDAR_GAP"


def test_two_fold_eligibility_at_26_months_and_shortfall_at_25():
    calendar = build_calendar(_two_symbol_payload("2024-01-01", 791))
    split = split_manifest(calendar)
    assert split["fold_count"] == 2
    assert add_months(date(2024, 1, 1), 18).isoformat() == split["folds"][1]["validation_end_exclusive"]

    short = build_calendar(_two_symbol_payload("2024-01-01", 762))
    with pytest.raises(ProtocolError) as exc:
        split_manifest(short)
    assert exc.value.code == "INSUFFICIENT_HISTORY"
    detail = exc.value.detail
    assert detail["folds_possible"] == 1
    assert detail["folds_required"] == 2
    assert detail["available_discovery_days"] == short["discovery"]["day_count"]
    assert detail["required_discovery_days"] == 547
    assert detail["shortfall_days"] == 14
    assert protocol_report(_two_symbol_payload("2024-01-01", 762))["status"] == "INSUFFICIENT_HISTORY"


def test_screen_control_spec_and_split_use_canonical_protocol():
    spec = control_spec("FAM-TASK12", 7)
    assert spec["draw_mapping"] == "HMAC_SHA256_COUNTER_V1"
    assert "NumPy PCG64 is never the control engine" in spec["seed_protocol_note"]
    assert spec["matched_on"] == ["calendar", "trade_count", "holding_time"]
    assert spec["variants"] == 12
    assert spec["family_size"] == 12

    split = screen_split_manifest(_days("2024-01-01", 791))
    assert split["protocol"]["train_months"] == 12
    assert split["fold_count"] == 2


def test_protocol_cli_emits_manifest_on_supplied_calendar(tmp_path, capsys):
    from qel.cli import main

    calendar_path = tmp_path / "calendar.json"
    calendar_path.write_text(json.dumps({"symbol": "BTC", "dates": _days("2024-01-01", 791)}))
    out = tmp_path / "protocol.json"
    code = main(["protocol", "--calendar", str(calendar_path), "--out", str(out)])
    assert code == 0
    payload = json.loads(out.read_text())
    assert payload["status"] == "PASS"
    assert payload["split_manifest"]["fold_count"] == 2
    assert payload["preregistration"]["family"]["family_size"] == 12
    assert payload["scope_checks"]["status"] == "PASS"

    short_path = tmp_path / "short.json"
    short_path.write_text(json.dumps({"symbol": "BTC", "dates": _days("2024-01-01", 20)}))
    assert main(["protocol", "--calendar", str(short_path), "--out", str(tmp_path / "short-out.json")]) == 3
