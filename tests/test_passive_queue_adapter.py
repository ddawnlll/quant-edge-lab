"""TASK-17 adapter-over-native completion evidence checks.

These tests validate `PASSIVE_QUEUE_ADAPTER_2026-11-10.json` against the
independent pure-Python fixture oracle. They also pin the profile boundary:
every behavior is a client-owned adapter semantic (`ADAPTER_OVER_NATIVE`), the
pinned native engine remains the exchange, and the native gaps stay recorded.

The evidence is never mutated and no expected value is used as an execution
input by the Rust runner.
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
EVIDENCE = ROOT / "evidence" / "labd" / "PASSIVE_QUEUE_ADAPTER_2026-11-10.json"
NATIVE_EVIDENCE = ROOT / "evidence" / "labd" / "PASSIVE_QUEUE_COMPLETION_PRE_ADAPTER_2026-11-10.json"
COMPLETION_EVIDENCE = ROOT / "evidence" / "labd" / "PASSIVE_QUEUE_COMPLETION_2026-11-10.json"
BINARY = qel_paths.find("qel-passive-runner")
PROFILE = "ADAPTER_OVER_NATIVE"
NAUTILUS_COMMIT = "27a8e54e7ac3c57d6cbf8891f0283dfbaee97317"


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
def payload(evidence: dict) -> dict:
    return evidence["probe_payload"]


@pytest.fixture(scope="module")
def probes(payload: dict) -> dict:
    return payload["probes"]


def oracle(fixture_id: str, scenario: str = "BASE") -> dict:
    return Harness().run_named(fixture_id, scenario).metrics()


def num(value) -> Fraction:
    assert value is not None, "expected a numeric value"
    return Fraction(str(value))


def without_elapsed(value):
    if isinstance(value, dict):
        return {k: without_elapsed(v) for k, v in value.items() if k != "elapsed_ms"}
    if isinstance(value, list):
        return [without_elapsed(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Evidence integrity and build provenance
# ---------------------------------------------------------------------------


def test_evidence_pins_the_native_engine(evidence: dict) -> None:
    assert evidence["profile"] == PROFILE
    assert evidence["pinned"]["tag"] == "v1.231.0"
    assert evidence["pinned"]["commit"] == NAUTILUS_COMMIT
    assert evidence["pinned"]["head_matches_expected"] is True
    assert evidence["base_sha"] == "ce91981055125328dae4b59add8e5432892c4f20"


def test_evidence_records_source_and_binary_hashes(evidence: dict) -> None:
    build = evidence["build"]
    assert build["crate"] == "native/passive_runner"
    assert build["profile"] == "release"
    assert build["exit_code"] == 0
    assert "cargo build --release" in build["command"]
    for source in build["sources"]:
        assert sha256(ROOT / source["path"]) == source["sha256"], source["path"]
    assert sha256(ROOT / build["cargo_toml"]["path"]) == build["cargo_toml"]["sha256"]
    assert len(build["binary"]["sha256"]) == 64


def test_binary_hash_matches_the_built_artifact(evidence: dict) -> None:
    if BINARY is None:
        pytest.skip("release binary not present in this environment")
    assert sha256(BINARY) == evidence["build"]["binary"]["sha256"]


def test_evidence_records_resources(evidence: dict) -> None:
    run = evidence["run"]
    assert run["exit_code"] == 0
    assert 0 <= run["peak_rss_kb"] < 2 * 1024 * 1024
    assert run["wall_ms"] >= 0
    assert evidence["probe_payload"]["elapsed_ms"] >= 0


def test_embedded_payload_is_self_consistent(evidence: dict, payload: dict) -> None:
    assert payload["probe"] == "qel-passive-queue-runner"
    assert payload["pinned"]["commit"] == evidence["pinned"]["commit"]
    assert canonical_sha256(payload) == evidence["run"]["probe_payload_sha256"]
    assert payload["adapter_summary"] == evidence["adapter_summary"]
    assert payload["summary"] == evidence["native_summary"]


def test_profile_boundary_is_explicit(evidence: dict, payload: dict) -> None:
    profile = payload["adapter_profile"]
    assert profile["name"] == PROFILE
    claimed = " ".join(profile["claimed"]).lower()
    for behavior in ("already_filled", "priority_reset_count", "epoch", "last_good_seq", "venue sequence", "oco"):
        assert behavior in claimed, behavior
    not_claimed = " ".join(profile["not_claimed"]).lower()
    for must_not in ("native venue capability", "matching fidelity", "persistence", "queue-state readback"):
        assert must_not in not_claimed, must_not
    boundary = payload["acceptance_boundary"]
    assert "PARTIAL" in boundary["claimed"]
    assert "does not change any native capability verdict" in boundary["adapter_note"]
    native_gaps = " ".join(boundary["not_claimed"])
    for fixture_id in ("F04", "F05", "F07", "F29", "F31"):
        assert fixture_id in native_gaps, fixture_id


def test_every_adapter_probe_names_the_native_limitation(probes: dict) -> None:
    for name, probe in probes.items():
        if not name.startswith("adapter_"):
            continue
        assert probe["profile"] == PROFILE, name
        assert probe["native_limitation"], name
        assert probe.get("profile_boundary"), name


def test_artifact_supersession_is_recorded(evidence: dict) -> None:
    supersession = evidence["artifact_supersession"]
    pre = supersession["pre_adapter_artifact"]
    regenerated = supersession["regenerated_artifact"]
    assert pre["path"] == "evidence/labd/PASSIVE_QUEUE_COMPLETION_PRE_ADAPTER_2026-11-10.json"
    assert sha256(ROOT / pre["path"]) == pre["sha256"]
    assert regenerated["path"] == "evidence/labd/PASSIVE_QUEUE_COMPLETION_2026-11-10.json"
    assert sha256(ROOT / regenerated["path"]) == regenerated["sha256"]
    assert regenerated["superseded_sha256"] == pre["sha256"]
    assert "regenerated" in supersession["reason"]
    assert supersession["date"] == "2026-11-10"
    assert "none" in supersession["behavioral_change"]

    # The regenerated completion evidence carries the new build and points at
    # the preserved artifact, so the historical record is honest.
    completion = json.loads(COMPLETION_EVIDENCE.read_text())
    assert completion["artifact_supersession"]["pre_adapter_artifact"]["sha256"] == pre["sha256"]
    assert completion["build"]["source"]["sha256"] == evidence["build"]["sources"][0]["sha256"]
    assert completion["build"]["binary"]["sha256"] == evidence["build"]["binary"]["sha256"]


# ---------------------------------------------------------------------------
# F04: explicit ALREADY_FILLED cancel response
# ---------------------------------------------------------------------------


def test_f04_adapter_matches_oracle(evidence: dict, probes: dict) -> None:
    probe = probes["adapter_f04_cancel_race"]
    expected = oracle("F04")
    assert probe["verdict"] == "PASS"
    assert num(probe["filled_qty"]) == num(expected["filled_qty"]) == 1
    assert probe["state"] == expected["state"] == "FILLED"
    assert probe["last_cancel_response"] == expected["last_cancel_response"] == "ALREADY_FILLED"
    assert evidence["oracle_comparison"]["F04"]["match"] is True


def test_f04_preserves_the_delayed_arrival_and_has_no_overfill(probes: dict) -> None:
    probe = probes["adapter_f04_cancel_race"]
    assert probe["delayed_cancel_arrival_reproduced"] is True
    assert probe["setup"]["cancel_sent_ns"] == 10
    assert probe["setup"]["cancel_arrival_ns"] == 30
    assert probe["setup"]["trade_ns"] == 20
    assert probe["adapter"]["cancel_requested"]["decision"] == "DISPATCH_TO_VENUE"
    assert probe["adapter"]["cancel_arrived"]["ts_event"] == 1_000_000_040
    assert probe["adapter"]["cancel_arrived"]["response"] == "ALREADY_FILLED"
    assert probe["adapter"]["cancel_response_count"] == 1
    assert probe["native"]["after_cancel_sent"]["status"] == "PendingCancel"
    assert probe["native"]["after_cancel_arrival"]["status"] == "Filled"
    assert num(probe["native"]["after_cancel_arrival"]["filled_qty"]) == 1
    assert probe["overfill"] is False
    assert probe["adapter"]["local_order"]["canceled_qty"] == 0


# ---------------------------------------------------------------------------
# F05: client-managed cancel+new replace and priority reset count
# ---------------------------------------------------------------------------


def test_f05_adapter_matches_oracle(evidence: dict, probes: dict) -> None:
    probe = probes["adapter_f05_replace"]
    expected = oracle("F05")
    assert probe["verdict"] == "PASS"
    assert num(probe["filled_qty"]) == num(expected["filled_qty"]) == 2
    assert num(probe["leaves_qty"]) == num(expected["leaves_qty"]) == 13
    assert num(probe["accepted_qty"]) == num(expected["accepted_qty"]) == 15
    assert num(probe["ahead_qty"]) == num(expected["ahead_qty"]) == 100
    assert probe["priority_reset_count"] == expected["priority_reset_count"] == 1
    assert evidence["oracle_comparison"]["F05"]["match"] is True


def test_f05_increase_is_cancel_new_and_decrease_is_in_place(probes: dict) -> None:
    probe = probes["adapter_f05_replace"]
    replaces = probe["adapter"]["replaces"]
    assert len(replaces) == 2
    increase, decrease = replaces
    assert increase["increase"] is True
    assert increase["priority_reset"] is True
    assert increase["new_order_id"] != increase["request_id"]
    assert num(increase["leaves_qty"]) == 18
    assert decrease["increase"] is False
    assert decrease["priority_reset"] is False
    assert decrease["new_order_id"] == decrease["request_id"]
    assert num(decrease["leaves_qty"]) == 13
    assert num(probe["adapter"]["canceled_original"]["canceled_qty"]) == 8


def test_f05_native_control_still_preserves_priority(probes: dict) -> None:
    control = probes["adapter_f05_replace"]["native_control"]
    assert control["observed_priority_behavior"] == "preserved_on_same_price_increase"
    assert control["native_matches_fixture_priority"] is False
    assert num(control["priority_probe_fill_after_increase_qty"]) > 0
    assert control["priority_reset_count_public"] is False
    assert control["ahead_qty_public"] is False
    assert "private" in control["missing_api"]


# ---------------------------------------------------------------------------
# F06 client profile: atomic client OCO, zero overfill, no reversal
# ---------------------------------------------------------------------------


def test_f06_client_profile_matches_oracle(evidence: dict, probes: dict) -> None:
    probe = probes["adapter_f06_client_oco"]
    expected = oracle("F06", "CLIENT_REDUCE_ONLY")
    assert probe["verdict"] == "PASS"
    assert num(probe["position_qty"]) == num(expected["position_qty"]) == 0
    assert probe["fill_count"] == expected["fill_count"] == 1
    assert probe["second_child_response"] == expected["second_child_response"] == "REDUCE_ONLY_REJECT"
    assert evidence["oracle_comparison"]["F06_CLIENT_REDUCE_ONLY"]["match"] is True


def test_f06_client_profile_has_no_overfill_or_reversal(probes: dict) -> None:
    probe = probes["adapter_f06_client_oco"]
    assert num(probe["entry_filled_qty"]) == 1
    assert num(probe["exit_total_qty"]) == 1
    assert num(probe["exit_qty_sized_from_filled"]) == 1
    assert probe["overfill"] is False
    assert probe["position_reversed"] is False
    assert probe["adapter"]["group_sibling_cancel"]["policy"] == "ATOMIC_CLIENT_OCO"
    assert probe["adapter"]["group_sibling_canceled"]["response"] == "CANCELED"
    assert probe["native"]["sl_after_sibling_cancel"]["status"] == "Canceled"
    assert probe["adapter"]["reduce_only_guard"]["venue_submission"] is False
    assert probe["native"]["second_child_native_control"]["status"] == "Rejected"


def test_f06_native_oco_is_unchanged(probes: dict) -> None:
    assert probes["f06_oco"]["verdict"] == "PASS"
    assert probes["f06_oco"]["oco_supported"] is True
    assert probes["adapter_f06_client_oco"]["native_oco_unchanged"] is True


# ---------------------------------------------------------------------------
# F07: position epochs and stale-epoch rejection before the venue
# ---------------------------------------------------------------------------


def test_f07_adapter_matches_oracle(evidence: dict, probes: dict) -> None:
    probe = probes["adapter_f07_epoch"]
    expected = oracle("F07")
    assert probe["verdict"] == "PASS"
    assert num(probe["new_epoch_qty"]) == num(expected["new_epoch_qty"]) == 1
    assert probe["stale_stop_response"] == expected["stale_stop_response"] == "STALE_POSITION_EPOCH"
    assert probe["stale_fill_count"] == expected["stale_fill_count"] == 0
    assert evidence["oracle_comparison"]["F07"]["match"] is True


def test_f07_stale_stop_never_reaches_the_venue(probes: dict) -> None:
    probe = probes["adapter_f07_epoch"]
    stale = probe["adapter"]["stale_stop"]
    assert stale["requested_epoch"] == 1
    assert stale["current_epoch"] == 2
    assert stale["response"] == "STALE_POSITION_EPOCH"
    assert stale["venue_submission"] is False
    assert probe["adapter"]["stale_order_registered"] is False
    assert probe["native"]["stale_stop_present"] is False
    assert probe["adapter"]["fresh_stop"]["response"] == "ACCEPTED"
    assert probe["native"]["fresh_stop_after_submit"]["status"] == "Accepted"
    # Native control: the same stop sent without the gate is accepted.
    assert probe["native"]["native_control_after_submit"]["status"] == "Accepted"
    assert "no epoch" in probe["native_limitation"]


# ---------------------------------------------------------------------------
# F29: book sequence high-water mark, INVALID state and resync
# ---------------------------------------------------------------------------


def test_f29_adapter_matches_oracle(evidence: dict, probes: dict) -> None:
    probe = probes["adapter_f29_book_sequence"]
    expected = oracle("F29")
    assert probe["verdict"] == "PASS"
    assert probe["initial_match"] == expected["initial_match"] is True
    assert probe["last_good_seq"] == expected["last_good_seq"] == "102"
    assert probe["book_state"] == expected["book_state"] == "INVALID"
    assert probe["absolute_updates_not_additive"] == expected["absolute_updates_not_additive"] is True
    assert evidence["oracle_comparison"]["F29"]["match"] is True


def test_f29_bad_delta_never_mutates_the_native_book(probes: dict) -> None:
    probe = probes["adapter_f29_book_sequence"]
    rejected = probe["adapter"]["rejected_delta"]
    assert rejected["code"] == "PREVIOUS_SEQUENCE_MISMATCH"
    assert rejected["forwarded_to_native_book"] is False
    assert probe["native_book"]["rejected_delta_applied"] is False
    assert probe["native_book"]["sequence_before_rejected_delta"] == 102
    assert probe["native_book"]["sequence_after_rejected_delta"] == 102
    assert num(probe["native_book"]["bid_99_level_qty"]) == 0
    assert probe["native_book"]["apply_errors"] == [None, None]


def test_f29_risk_is_blocked_then_resynced(probes: dict) -> None:
    probe = probes["adapter_f29_book_sequence"]
    assert probe["adapter"]["risk_gate"]["code"] == "BOOK_RESYNC_REQUIRED"
    assert probe["adapter"]["risk_gate"]["blocked_risk_requests"] == 1
    assert probe["adapter"]["resync"]["state_after"] == "VALID"
    assert probe["adapter"]["resync"]["risk_allowed"] is True
    assert probe["adapter"]["resync"]["delta_after_resync"] == "APPLIED"


# ---------------------------------------------------------------------------
# F31: venue sequencing, duplicate/out-of-order suppression, canceled leaves
# ---------------------------------------------------------------------------


def test_f31_adapter_matches_oracle(evidence: dict, probes: dict) -> None:
    probe = probes["adapter_f31_duplicate_fill"]
    expected = oracle("F31")
    assert probe["verdict"] == "PASS"
    assert num(probe["accepted_qty"]) == num(expected["accepted_qty"]) == 2
    assert num(probe["filled_qty"]) == num(expected["filled_qty"]) == 1
    assert num(probe["leaves_qty"]) == num(expected["leaves_qty"]) == 0
    assert num(probe["canceled_qty"]) == num(expected["canceled_qty"]) == 1
    assert probe["duplicate_funding_or_fee_debits"] == expected["duplicate_funding_or_fee_debits"] == 0
    assert probe["state"] == expected["state"] == "CANCELED"
    assert evidence["oracle_comparison"]["F31"]["match"] is True


def test_f31_event_trace_is_deduplicated_and_ordered(probes: dict) -> None:
    trace = probes["adapter_f31_duplicate_fill"]["adapter"]["event_trace"]
    assert len(trace["fill"]) == 1
    assert trace["fill"][0]["verdict"] == "APPLIED"
    assert trace["fill"][0]["venue_seq"] == 2
    assert trace["delayed_ack"]["venue_seq"] == 1
    assert trace["delayed_ack"]["verdict"] == "OUT_OF_ORDER_SUPPRESSED"
    assert trace["duplicate_fill"]["verdict"] == "DUPLICATE_SUPPRESSED"
    assert trace["duplicate_fill"]["applied_qty"] == 0
    assert trace["cancel_arrived"]["response"] == "CANCELED"
    assert probes["adapter_f31_duplicate_fill"]["strategy_fill_events"] == 1
    assert probes["adapter_f31_duplicate_fill"]["adapter"]["duplicate_fills_suppressed"] == 1


def test_f31_native_leaves_behaviour_is_recorded(probes: dict) -> None:
    probe = probes["adapter_f31_duplicate_fill"]
    assert num(probe["leaves_qty"]) == 0
    assert num(probe["native_leaves_qty_after_cancel"]) == 1
    assert probe["native"]["venue_seq_field_present"] is False
    assert probe["native"]["local_order_event_hook_present"] is False
    assert "no venue_seq" in probe["native_limitation"]


# ---------------------------------------------------------------------------
# Unknown aggressor: conservative queue, explicit profile difference
# ---------------------------------------------------------------------------


def test_unknown_aggressor_is_conservative_and_documented(probes: dict) -> None:
    probe = probes["adapter_f02_unknown_aggressor"]
    assert probe["verdict"] == "PARTIAL"
    assert probe["pass"] is True
    assert num(probe["adapter"]["ahead_after_known_trade"]) == 20
    assert num(probe["adapter"]["ahead_after_unknown_trade"]) == 20
    assert num(probe["adapter"]["fill_on_unknown_aggressor_qty"]) == 0
    assert probe["adapter"]["policy"] == "CONSERVATIVE_NO_DEPLETION"
    assert probe["profile_difference"] is True
    assert num(probe["native_fill_on_unknown_aggressor_qty"]) > 0
    assert "NoAggressor" in probe["native_limitation"]


# ---------------------------------------------------------------------------
# Existing native verdicts are unchanged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture_id",
    ["F02", "F02+CANCEL_REMAINDER", "F02+depth-once", "F02+SHARED_LEVEL", "F03", "F06", "F33_FOK", "F33_STOP_LIMIT_GAP", "F34"],
)
def test_existing_native_pass_verdicts_are_unchanged(payload: dict, fixture_id: str) -> None:
    assert payload["summary"][fixture_id] == "PASS"


def test_existing_native_partial_and_blocked_verdicts_are_unchanged(payload: dict) -> None:
    summary = payload["summary"]
    assert summary["F04"] == "PARTIAL"
    assert summary["F05"] == "PARTIAL"
    assert summary["F31"] == "PARTIAL"
    assert summary["F29"] == "BLOCKED_BACKEND"
    assert summary["F07"] == "BLOCKED_BACKEND"
    assert summary["F06_CLIENT_UNSAFE_OBSERVABLE"] == "PARTIAL"


def test_existing_native_probes_are_byte_identical(probes: dict) -> None:
    if not NATIVE_EVIDENCE.exists():
        pytest.skip("pre-adapter native evidence not present")
    prior = json.loads(NATIVE_EVIDENCE.read_text())["probe_payload"]["probes"]
    for name, old in prior.items():
        assert without_elapsed(old) == without_elapsed(probes[name]), name


def test_native_capability_verdict_stays_partial(evidence: dict) -> None:
    verdict = (ROOT / "locks" / "CAPABILITY_VERDICT.md").read_text()
    assert "CAP-L2-QUEUE" in verdict
    assert "PARTIAL" in verdict
    assert evidence["probe_payload"]["acceptance_boundary"]["not_claimed"]


def test_adapter_does_not_claim_native_capabilities(evidence: dict, payload: dict) -> None:
    boundary = payload["adapter_profile"]["not_claimed"]
    joined = " ".join(boundary).lower()
    assert "as a native venue capability" in joined
    for fixture_id in ("F04", "F05", "F07", "F29", "F31"):
        entry = evidence["per_fixture"][fixture_id]
        assert entry["profile"] == PROFILE
        assert entry["native_limitation"]
