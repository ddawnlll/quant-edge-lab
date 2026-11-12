"""TASK-17 passive queue completion evidence checks.

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

import qel_paths
from qel.conformance import Harness

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence" / "labd" / "PASSIVE_QUEUE_COMPLETION_2026-11-10.json"
BINARY = qel_paths.find("qel-passive-runner")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_sha256(value) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


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
    assert sha256(ROOT / build["cargo_toml"]["path"]) == build["cargo_toml"]["sha256"]


def test_binary_hash_matches_built_artifact_if_present(evidence: dict) -> None:
    if BINARY is None:
        pytest.skip("release binary not present in this environment")
    assert sha256(BINARY) == evidence["build"]["binary"]["sha256"]


def test_evidence_records_resources(evidence: dict) -> None:
    run = evidence["run"]
    assert run["exit_code"] == 0
    assert run["peak_rss_kb"] < 2 * 1024 * 1024
    assert run["wall_ms"] >= 0
    assert evidence["probe_payload"]["elapsed_ms"] >= 0


def test_embedded_payload_is_self_consistent(evidence: dict) -> None:
    payload = evidence["probe_payload"]
    assert payload["probe"] == "qel-passive-queue-runner"
    assert payload["pinned"]["commit"] == evidence["pinned"]["commit"]
    assert canonical_sha256(payload) == evidence["run"]["probe_payload_sha256"]
    assert payload["summary"] == evidence["summary"]


def test_acceptance_boundary_is_explicit(evidence: dict) -> None:
    boundary = evidence["acceptance_boundary"]
    assert "PARTIAL" in boundary["claimed"]
    joined = " ".join(boundary["not_claimed"]).lower()
    for must_not_claim in (
        "queue state readback",
        "unknown aggressor",
        "resync",
        "localview",
        "f07",
        "f29",
        "f31",
    ):
        assert must_not_claim in joined, must_not_claim
    unchanged = " ".join(boundary["unchanged"]).lower()
    assert "taker l1 funding certificate" in unchanged
    assert "cap-l2-queue remains partial" in unchanged


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
# Time in force, stop-limit, replace, native OCO
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


def test_f05_native_replace_preserves_priority_on_same_price_increase(probes: dict) -> None:
    f05 = probes["f05_replace"]
    # The fixture expects an increase to reset priority (priority_reset_count=1);
    # the native engine preserves the tracked queue on a same-price size change.
    assert f05["observed_priority_behavior"] == "preserved_on_same_price_increase"
    assert f05["native_matches_fixture_priority"] is False
    assert num(f05["priority_probe_fill_after_increase_qty"]) > 0
    assert f05["priority_reset_count_public"] is False
    assert f05["ahead_qty_public"] is False
    assert "private" in f05["missing_api"]


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
# Remaining checks: F04, F29, F31, client-managed OCO
# ---------------------------------------------------------------------------


def test_f04_delayed_cancel_race_has_no_overfill(probes: dict) -> None:
    f04 = probes["f04_cancel_race"]
    assert f04["verdict"] == "PARTIAL"
    assert f04["pass"] is True
    assert f04["delayed_cancel_arrival_reproduced"] is True
    # Cancel is sent at 10 with delete latency 20, so it arrives at 30.
    assert f04["setup"]["cancel_sent_ns"] == 10
    assert f04["setup"]["cancel_arrival_ns"] == 30
    assert f04["setup"]["trade_ns"] == 20
    assert f04["after_cancel_sent"]["status"] == "PendingCancel"
    assert num(f04["final_filled_qty"]) == 1
    assert num(f04["final_canceled_qty"]) == 0
    assert f04["after_cancel_arrival"]["status"] == "Filled"
    assert f04["overfill"] is False
    # The fixture's ALREADY_FILLED cancel response is not emitted.
    assert f04["last_cancel_response"] is None
    assert f04["cancel_response_api_present"] is False
    assert f04["cancel_rejections"] == []


def test_f29_book_gap_is_not_detected(probes: dict) -> None:
    f29 = probes["f29_book_sequence"]
    assert f29["verdict"] == "BLOCKED_BACKEND"
    assert f29["sequence_stream"] == [100, 101, 102, 110, 105]
    assert f29["gap_detected"] is False
    assert f29["out_of_order_detected"] is False
    assert f29["apply_errors"] == [None, None, None, None, None]
    assert f29["sequence_high_water"] == 110
    assert f29["absolute_updates_not_additive"] is True
    assert f29["book_state_api_present"] is False
    assert f29["last_good_seq_api_present"] is False


def test_f31_duplicate_fill_is_suppressed_by_oms(probes: dict) -> None:
    f31 = probes["f31_duplicate_fill"]
    assert f31["verdict"] == "PARTIAL"
    assert f31["pass"] is True
    assert f31["duplicate_fill_predicate"] is True
    assert f31["duplicate_suppressed"] is True
    assert f31["strategy_fill_events_before_injection"] == 1
    assert f31["strategy_fill_events_after_injection"] == 1
    assert num(f31["accepted_qty"]) == 2
    assert num(f31["filled_qty"]) == 1
    assert num(f31["canceled_qty"]) == 1
    assert f31["state"] == "Canceled"
    assert f31["duplicate_fee_or_funding_debits"] == 0
    # Native keeps the unfilled leaves after a cancel; the fixture expects 0.
    assert f31["native_leaves_qty_after_cancel"] == 1
    assert f31["venue_seq_field_present"] is False
    assert f31["local_order_event_data_hook_present"] is False


def test_client_managed_oco_can_overfill(probes: dict) -> None:
    cm = probes["f06_client_unsafe_observable"]
    assert cm["verdict"] == "PARTIAL"
    assert cm["pass"] is True
    assert num(cm["entry_filled_qty"]) == 1
    assert num(cm["stop_filled_qty"]) == 1
    assert num(cm["tp_filled_qty"]) == 1
    assert num(cm["exit_total_qty"]) == 2
    assert cm["overfill_observed"] is True
    assert num(cm["over_exit_qty"]) == 1
    assert num(cm["position_qty_after"]) == -1
    assert cm["position_side_after"] == "Short"
    assert cm["position_reversed"] is True
    assert cm["native_oco_sibling_cancelled"] is False


# ---------------------------------------------------------------------------
# Honest BLOCKED records
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture_id", ["F07", "F29"])
def test_unreachable_capabilities_are_blocked_with_exact_api(evidence: dict, fixture_id: str) -> None:
    fixture = evidence["per_fixture"][fixture_id]
    assert fixture["verdict"] == "BLOCKED_BACKEND"
    missing = fixture.get("missing_api") or fixture.get("missing")
    assert missing, f"{fixture_id} must record the exact missing API"


def test_f07_records_the_missing_epoch_field(evidence: dict) -> None:
    f07 = evidence["per_fixture"]["F07"]
    assert f07["verdict"] == "BLOCKED_BACKEND"
    assert "no epoch field" in f07["missing_api"]
    assert "epoch" in f07["missing_field"]
    assert "STALE_POSITION_EPOCH" in f07["missing_error"]


def test_f04_records_the_missing_cancel_response(evidence: dict) -> None:
    f04 = evidence["per_fixture"]["F04"]
    assert f04["verdict"] == "PARTIAL"
    assert "ALREADY_FILLED" in f04["missing"]
    assert "process_cancel" in f04["missing"]
    assert f04["fixture_assertion_not_met"] == "last_cancel_response == ALREADY_FILLED"


def test_partial_fixtures_record_exact_gaps(evidence: dict) -> None:
    assert evidence["per_fixture"]["F05"]["verdict"] == "PARTIAL"
    assert evidence["per_fixture"]["F31"]["verdict"] == "PARTIAL"
    assert evidence["per_fixture"]["F06_CLIENT_UNSAFE_OBSERVABLE"]["verdict"] == "PARTIAL"
    assert "priority_reset_count" in evidence["per_fixture"]["F05"]["missing"]
    assert "LOCAL_ORDER_EVENT" in evidence["per_fixture"]["F31"]["missing"]
    assert evidence["per_fixture"]["F06_CLIENT_UNSAFE_OBSERVABLE"]["overfill_observed"] is True


def test_capability_verdict_stays_partial(evidence: dict) -> None:
    verdict = (ROOT / "locks" / "CAPABILITY_VERDICT.md").read_text()
    assert "CAP-L2-QUEUE" in verdict
    assert "PARTIAL" in verdict
    assert evidence["probe_payload"]["acceptance_boundary"]["not_claimed"]
