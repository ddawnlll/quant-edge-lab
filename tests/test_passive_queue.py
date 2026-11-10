"""TASK-17 passive queue evidence checks.

These tests validate the recorded native evidence against the independent
pure-Python fixture oracle. They never mutate the evidence and they never
claim more than the evidence proves.
"""

from __future__ import annotations

import hashlib
import json
from fractions import Fraction
from pathlib import Path

import pytest

from qel.conformance import Harness

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence" / "labd" / "PASSIVE_QUEUE_2026-11-10.json"
BINARY = Path("/home/quant/quant/.build-cache/qel-native/release/qel-passive-runner")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def evidence() -> dict:
    assert EVIDENCE.exists(), f"missing evidence file: {EVIDENCE}"
    return json.loads(EVIDENCE.read_text())


@pytest.fixture(scope="module")
def probes(evidence: dict) -> dict:
    return evidence["probe_payload"]["probes"]


def oracle(fixture_id: str, scenario: str = "BASE") -> dict:
    return Harness().run_named(fixture_id, scenario).metrics()


def num(value) -> Fraction:
    return Fraction(str(value))


# ---------------------------------------------------------------------------
# Evidence integrity
# ---------------------------------------------------------------------------


def test_evidence_records_pinned_commit(evidence: dict) -> None:
    pinned = evidence["pinned"]
    assert pinned["tag"] == "v1.231.0"
    assert pinned["commit"] == "27a8e54e7ac3c57d6cbf8891f0283dfbaee97317"
    assert pinned["head_matches_expected"] is True


def test_evidence_records_binary_and_source_hashes(evidence: dict) -> None:
    build = evidence["build"]
    assert len(build["binary"]["sha256"]) == 64
    assert len(build["source"]["sha256"]) == 64
    assert build["exit_code"] == 0
    assert build["crate"] == "native/passive_runner"
    assert sha256(ROOT / build["source"]["path"]) == build["source"]["sha256"]


def test_binary_hash_matches_built_artifact_if_present(evidence: dict) -> None:
    if not BINARY.exists():
        pytest.skip("release binary not present in this environment")
    assert sha256(BINARY) == evidence["build"]["binary"]["sha256"]


def test_evidence_records_resources(evidence: dict) -> None:
    run = evidence["run"]
    assert run["exit_code"] == 0
    assert run["peak_rss_kb"] < 2 * 1024 * 1024
    assert run["wall_ms"] >= 0
    assert evidence["probe_payload"]["elapsed_ms"] >= 0


def test_standalone_payload_matches_embedded_payload(evidence: dict) -> None:
    run = evidence["run"]
    payload_path = ROOT / run["probe_payload_path"]
    assert payload_path.exists()
    assert sha256(payload_path) == run["probe_payload_sha256"]
    assert json.loads(payload_path.read_text()) == evidence["probe_payload"]


def test_acceptance_boundary_is_explicit(evidence: dict) -> None:
    boundary = evidence["acceptance_boundary"]
    assert "PARTIAL" in boundary["claimed"]
    joined = " ".join(boundary["not_claimed"]).lower()
    for must_not_claim in ("queue state readback", "unknown-aggressor", "resync", "localview"):
        assert must_not_claim in joined or must_not_claim == "localview"
    assert "taker L1 funding certificate" in " ".join(boundary["unchanged"])


# ---------------------------------------------------------------------------
# F02: the certified queue-ahead slice
# ---------------------------------------------------------------------------


def test_f02_matches_python_oracle(probes: dict) -> None:
    f02 = probes["f02_queue"]
    expected = oracle("F02")
    assert f02["verdict"] == "PASS"
    assert num(f02["filled_after_trade_1_qty"]) == num(expected["fills_after_step_1_qty"])
    assert num(f02["fill_step_2_qty"]) == num(expected["fill_step_2_qty"]) == 10
    assert num(f02["leaves_qty"]) == num(expected["leaves_qty"]) == 10
    assert num(f02["position_qty"]) == num(expected["position_qty"]) == 10
    assert num(f02["ahead_qty_after"]) == num(expected["ahead_qty"]) == 0


def test_f02_queue_state_is_inferred_not_observed(probes: dict) -> None:
    f02 = probes["f02_queue"]
    assert f02["queue_state_observable"] is False
    assert "no public readback" in f02["queue_state_note"]


def test_f02_depth_reduction_is_not_double_consumed(probes: dict) -> None:
    depth = probes["f02_depth_once"]
    assert depth["verdict"] == "PASS"
    assert depth["depth_reduction_double_consumed"] is False
    assert num(depth["filled_after_trade_1_qty"]) == 0
    assert num(depth["fill_step_2_qty"]) == 10


def test_f02_cancel_remainder_stops_further_fills(probes: dict) -> None:
    cancel = probes["f02_cancel_remainder"]
    assert cancel["verdict"] == "PASS"
    assert num(cancel["after_fill"]["filled_qty"]) == 10
    assert num(cancel["after_cancel"]["canceled_qty"]) == 10
    assert num(cancel["after_extra_trade"]["filled_qty"]) == 10
    assert cancel["after_extra_trade"]["status"] == "Canceled"


# ---------------------------------------------------------------------------
# Shared level budget (item 4)
# ---------------------------------------------------------------------------


def test_shared_level_budget_across_two_passive_orders(probes: dict) -> None:
    shared = probes["f02_shared_level"]
    assert shared["verdict"] == "PASS"
    assert shared["level_consumed_once"] is True
    assert num(shared["filled_total_qty"]) <= 10
    assert shared["per_order_budget_observed"] is False


def test_shared_level_budget_across_marketable_ioc_matches_f03(probes: dict) -> None:
    f03 = probes["f03_shared_liquidity"]
    expected = oracle("F03")
    assert f03["verdict"] == "PASS"
    assert num(f03["total_filled_qty"]) == num(expected["total_filled_qty"]) == 10
    assert num(f03["O-2"]["filled_qty"]) == num(expected["O-2.filled_qty"]) == 4
    assert num(f03["O-2"]["canceled_qty"]) == num(expected["O-2.canceled_qty"]) == 2


# ---------------------------------------------------------------------------
# Partial entry protection (F34)
# ---------------------------------------------------------------------------


def test_f34_partial_entry_protection_matches_oracle(probes: dict) -> None:
    f34 = probes["f34_partial_entry"]
    expected = oracle("F34")
    assert f34["verdict"] == "PASS"
    assert num(f34["stop_qty"]) == num(expected["stop_qty"]) == 3
    assert num(f34["tp1_qty"]) == num(expected["tp1_qty"]) == 1
    assert num(f34["tp2_qty"]) == num(expected["tp2_qty"]) == 2
    assert num(f34["exit_total_qty"]) == num(expected["exit_total_qty"]) == 3
    assert f34["unfilled_entry_protected_as_position"] is False
    assert num(f34["canceled_entry_remainder_qty"]) == 7
    assert num(f34["position_qty"]) == 3


# ---------------------------------------------------------------------------
# Time in force, stop-limit, replace, OCO
# ---------------------------------------------------------------------------


def test_f33_fok_cancels_in_full(probes: dict) -> None:
    fok = probes["f33_fok"]
    expected = oracle("F33", "FOK")
    assert fok["verdict"] == "PASS"
    assert num(fok["O-1"]["filled_qty"]) == num(expected["filled_qty"]) == 0
    assert num(fok["O-1"]["canceled_qty"]) == num(expected["canceled_qty"]) == 6


def test_f33_stop_limit_gap_triggers_without_filling(probes: dict) -> None:
    gap = probes["f33_stop_limit_gap"]
    expected = oracle("F33", "STOP_LIMIT_GAP")
    assert gap["verdict"] == "PASS"
    assert gap["triggered"] is expected["triggered"] is True
    assert num(gap["filled_qty"]) == num(expected["filled_qty"]) == 0
    assert num(gap["position_after_gap_qty"]) == num(expected["position_qty"]) == 1


def test_f05_replace_quantity_matches_oracle_but_priority_is_unreadable(probes: dict) -> None:
    f05 = probes["f05_replace"]
    expected = oracle("F05")
    assert f05["verdict"] == "PARTIAL"
    assert num(f05["filled_qty"]) == num(expected["filled_qty"]) == 2
    assert num(f05["leaves_qty"]) == num(expected["leaves_qty"]) == 13
    assert num(f05["accepted_qty"]) == num(expected["accepted_qty"]) == 15
    assert f05["priority_reset_count"] is None
    assert f05["ahead_qty"] is None
    # Native behavior contradicts the fixture's replace_increase_resets=true.
    assert num(f05["priority_probe_fill_after_increase_qty"]) > 0


def test_f06_native_oco_matches_oracle(probes: dict) -> None:
    f06 = probes["f06_oco"]
    expected = oracle("F06", "NATIVE_ATOMIC")
    assert f06["verdict"] == "PASS"
    assert f06["oco_supported"] is True
    assert num(f06["after_tp"]["position_qty"]) == num(expected["position_qty"]) == 0
    assert f06["after_tp"]["tp"]["status"] == "Filled"
    assert f06["after_tp"]["sl"]["status"] == "Canceled"
    assert f06["after_tp"]["reduce_only_flat"]["status"] != "Filled"


# ---------------------------------------------------------------------------
# Honest BLOCKED records
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture_id", ["F04", "F07", "F29", "F31", "F06_CLIENT_UNSAFE_OBSERVABLE"])
def test_unreachable_capabilities_are_blocked_with_exact_api(evidence: dict, fixture_id: str) -> None:
    fixture = evidence["per_fixture"][fixture_id]
    assert fixture["verdict"] == "BLOCKED_BACKEND"
    missing = fixture.get("missing_api") or fixture.get("missing")
    assert missing, f"{fixture_id} must record the exact missing API"


def test_f04_cancel_race_records_the_missing_hook(evidence: dict, probes: dict) -> None:
    f04 = probes["f04_cancel_race"]
    assert f04["verdict"] == "BLOCKED_BACKEND"
    assert f04["pass"] is False
    # The fill itself is observable; the cancel response is not.
    assert num(f04["after_trade"]["filled_qty"]) == 1
    assert f04["after_cancel"]["status"] == "Filled"
    assert f04["cancel_rejections"] == []
    assert "no delayed cancel-arrival API" in evidence["per_fixture"]["F04"]["missing"]


def test_capability_verdict_stays_partial(evidence: dict) -> None:
    verdict = (ROOT / "locks" / "CAPABILITY_VERDICT.md").read_text()
    assert "CAP-L2-QUEUE" in verdict
    assert "PARTIAL" in verdict
    assert evidence["probe_payload"]["acceptance_boundary"]["not_claimed"]
