"""TASK-16 signed scientific evaluator release tests.

Every scenario uses a synthetic cohort. It is a certification of the evaluator
protocol, not a market result and not a fresh OOS confirmation.
"""

from __future__ import annotations

import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from qel.contract.canonical import canonical_bytes, hash_jcs
from qel.evaluator.core import SealedError, SealedRegistry
from qel.evaluator.gates import (
    GATE_REQUIREMENTS,
    evaluate_gates,
    gate_result_is_complete,
    validate_gate_evidence,
)

PROGRAM = "PROG-RELEASE"
STRONG = [2.5 + (index % 5 - 2) * 0.05 for index in range(60)]
NULL = [0.02 * ((index % 7) - 3) for index in range(60)]
CONTROL = [1.2 + (index % 5 - 2) * 0.04 for index in range(60)]
INVALID = [0.1]


def _run_key(candidate_id: str, daily: list[float]) -> str:
    return hash_jcs({"candidate_id": candidate_id, "daily": [float(value) for value in daily]})


def _setup(
    tmp_path,
    *,
    name: str = "sealed",
    cohort_id: str = "COHORT-0",
    index: int = 0,
    candidate_keys: list[str] | None = None,
    trust: bool = True,
) -> SealedRegistry:
    registry = SealedRegistry(str(tmp_path / name))
    if trust:
        registry.trust_key(registry.public_key_hex(), "test-evaluator")
    registry.freeze_cohort(
        cohort_id,
        PROGRAM,
        index,
        candidate_keys or ["a" * 64],
        hash_jcs({"protocol": PROGRAM}),
        hash_jcs({"split": PROGRAM}),
        hash_jcs({"verifier": PROGRAM}),
        [hash_jcs({"certificate": PROGRAM})],
    )
    registry.prepare_read_receipt(cohort_id)
    registry.spend_consumption(cohort_id, acknowledged=True)
    return registry


def _gate_evidence(
    code_hash: str,
    *,
    synthetic: bool = True,
    failed_gate: str | None = None,
    extra: dict | None = None,
) -> dict:
    gates = {
        "G0": {
            "preregistration.valid": True,
            "capital.minimum_order_feasible": True,
            "quality.required_coverage": True,
        },
        "G1": {
            "critical_fixtures.failed": 0,
            "accounting.unexplained_residual_coeff": 0,
        },
        "G2": {
            "walkforward.protocol_valid": True,
            "development.economic_effect_supported": True,
            "controls.superiority_supported": True,
            "parameter_stability.frozen_rule_pass": True,
        },
        "G3": {
            "capabilities.all_required_certified": True,
            "execution.uncertainty_budget_pass": True,
            "capacity.envelope_pass": True,
        },
        "G4": {
            "independent_audit.pass": True,
            "statistics.calibration_gate_pass": True,
        },
    }
    if failed_gate == "G2":
        gates["G2"]["controls.superiority_supported"] = False
    if failed_gate == "G0":
        gates["G0"]["preregistration.valid"] = False
    if extra:
        for gate_id, fields in extra.items():
            gates.setdefault(gate_id, {}).update(fields)
    return {
        "source": "SYNTHETIC_EVALUATOR_CALIBRATION_VECTOR",
        "synthetic": synthetic,
        "code_hash": code_hash,
        "gates": gates,
    }


def _manifest(
    registry: SealedRegistry,
    cohort_id: str,
    candidate_ids: list[str],
    *,
    contaminated: bool = False,
    development_source: bool = False,
) -> dict:
    cohort = registry._cohort_row(cohort_id)
    return {
        "cohort_id": cohort_id,
        "window_commitment": cohort[3],
        "split_hash": cohort[9],
        "synthetic": True,
        "source": "SYNTHETIC_EVALUATOR_CALIBRATION_VECTOR",
        "oos_window": ["2026-01-01", "2026-06-30"],
        "development_windows": (
            [["2025-07-01", "2026-03-31"]] if contaminated else [["2024-01-01", "2024-12-31"]]
        ),
        "candidate_sources": {
            candidate_id: (
                "DEVELOPMENT" if development_source else "SYNTHETIC_OOS"
            )
            for candidate_id in candidate_ids
        },
        "control_source": "DEVELOPMENT" if development_source else "SYNTHETIC_OOS",
    }


def _independent_verify(batch: dict) -> None:
    """Verify with only the trusted public key and canonical JCS bytes."""
    body = {key: value for key, value in batch.items() if key not in ("signature", "public_key")}
    public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(batch["public_key"]))
    public_key.verify(bytes.fromhex(batch["signature"]), canonical_bytes(body))


def _anchor_events(registry: SealedRegistry, event_type: str) -> list[dict]:
    return [
        record["payload"]
        for record in registry.anchor.records()
        if record["payload"].get("type") == event_type
    ]


def _batch_count(registry: SealedRegistry) -> int:
    return registry.db.execute("SELECT COUNT(*) FROM batches").fetchone()[0]


# --------------------------------------------------------- caller metrics ---
def test_forged_caller_metrics_are_rejected(tmp_path):
    registry = _setup(tmp_path)
    code_hash = registry._cohort_row("COHORT-0")[10]
    manifest = _manifest(registry, "COHORT-0", ["C-STRONG"])

    for forged in (
        {"candidate_id": "C-STRONG", "daily": STRONG, "p_iut": 0.001},
        {"candidate_id": "C-STRONG", "daily": STRONG, "verdict": "EDGE"},
        {"candidate_id": "C-STRONG", "daily": STRONG, "mean_daily_return": 9.0},
        {"candidate_id": "C-STRONG", "daily": STRONG, "p_value": 0.0},
        {"candidate_id": "C-STRONG", "daily": STRONG, "code": "print('x')"},
        {"candidate_id": "C-STRONG", "daily": STRONG, "candidate_hash": "0" * 64},
    ):
        with pytest.raises(SealedError) as exc:
            registry.release_batch(
                "COHORT-0",
                [forged],
                CONTROL,
                capital=1000,
                economic_minimum=0.0001,
                b=64,
                gate_evidence=_gate_evidence(code_hash),
                data_manifest=manifest,
            )
        assert exc.value.code == "CALLER_METRICS_REJECTED"

    assert _batch_count(registry) == 0
    assert _anchor_events(registry, "BatchReleased") == []


def test_gate_evidence_cannot_supply_decision_metrics(tmp_path):
    registry = _setup(tmp_path)
    code_hash = registry._cohort_row("COHORT-0")[10]
    forged = _gate_evidence(code_hash, extra={"G2": {"p_iut": 0.001}})
    with pytest.raises(SealedError) as exc:
        validate_gate_evidence(forged, code_hash)
    assert exc.value.code == "CALLER_METRICS_REJECTED"

    with pytest.raises(SealedError) as exc:
        registry.record_override(
            "COHORT-0",
            {"verdict": "EDGE"},
            authorized_by="operator",
            reason="forged",
        )
    assert exc.value.code == "CALLER_METRICS_REJECTED"


# ----------------------------------------------------------- signed code ----
def test_unsigned_code_and_invalid_lineage_are_rejected(tmp_path):
    registry = SealedRegistry(str(tmp_path / "sealed"))
    valid = {
        "program_id": PROGRAM,
        "cohort_index": 0,
        "candidate_run_keys": ["a" * 64],
        "protocol_hash": "b" * 64,
        "split_hash": "c" * 64,
        "verifier_binary_hash": "d" * 64,
        "certificate_hashes": ["e" * 64],
    }
    for field, value in (
        ("verifier_binary_hash", ""),
        ("verifier_binary_hash", "not-a-hash"),
        ("protocol_hash", "zz"),
        ("split_hash", ""),
        ("certificate_hashes", []),
        ("candidate_run_keys", []),
    ):
        payload = {**valid, field: value}
        with pytest.raises(SealedError) as exc:
            registry.freeze_cohort("BAD", **payload)
        assert exc.value.code == "INVALID_LINEAGE"

    prepared = _setup(tmp_path, name="sealed-2")
    code_hash = prepared._cohort_row("COHORT-0")[10]
    mismatched = _gate_evidence(hash_jcs({"other": "code"}))
    with pytest.raises(SealedError) as exc:
        validate_gate_evidence(mismatched, code_hash)
    assert exc.value.code == "UNSIGNED_CODE"

    missing_code = _gate_evidence(code_hash)
    missing_code.pop("code_hash")
    with pytest.raises(SealedError) as exc:
        validate_gate_evidence(missing_code, code_hash)
    assert exc.value.code == "MISSING_GATE_FIELDS"


# --------------------------------------------------------------- G0-G4 ------
def test_g0_g4_missing_fields_fail_closed():
    assert evaluate_gates(None)["all_pass"] is False
    assert evaluate_gates(None)["status"] == "NOT_PROVIDED"

    code_hash = hash_jcs({"code": "synthetic"})
    complete = _gate_evidence(code_hash)
    result = evaluate_gates(
        complete,
        derived={
            "G1": {"lineage.valid": True},
            "G4": {
                "cohort.frozen_before_access": True,
                "window.valid_uncontaminated_spent_receipt": True,
                "holm.conservative_p_rejected_null": True,
                "economic_effect.both_minima_pass": True,
            },
        },
    )
    assert result["all_pass"] is True
    assert result["first_block"] is None
    assert gate_result_is_complete(result) is True
    assert result["synthetic"] is True

    incomplete = _gate_evidence(code_hash)
    del incomplete["gates"]["G2"]["controls.superiority_supported"]
    with pytest.raises(SealedError) as exc:
        validate_gate_evidence(incomplete, code_hash)
    assert exc.value.code == "MISSING_GATE_FIELDS"

    derived_false = {
        "G1": {"lineage.valid": True},
        "G4": {
            "cohort.frozen_before_access": True,
            "window.valid_uncontaminated_spent_receipt": True,
            "holm.conservative_p_rejected_null": True,
            "economic_effect.both_minima_pass": True,
        },
    }
    with pytest.raises(SealedError) as exc:
        validate_gate_evidence(
            _gate_evidence(code_hash, extra={"G4": {"holm.conservative_p_rejected_null": False}}),
            code_hash,
        )
    # A caller may not supply an authoritative field at all; it is unknown.
    assert exc.value.code in ("MISSING_GATE_FIELDS", "CALLER_METRICS_REJECTED")

    conflicted = evaluate_gates(
        _gate_evidence(code_hash),
        derived={**derived_false, "G4": {**derived_false["G4"], "holm.conservative_p_rejected_null": False}},
    )
    # The caller omitted the authoritative field, so the derived False blocks.
    assert conflicted["all_pass"] is False
    assert conflicted["gates"]["G4"]["status"] == "BLOCK"

    dependency = evaluate_gates(
        _gate_evidence(code_hash, failed_gate="G0"),
        derived=derived_false,
    )
    assert dependency["first_block"] == "G0"
    assert dependency["gates"]["G1"]["reason"] == "DEPENDENCY_BLOCKED"


def test_release_without_gate_evidence_is_not_promotable(tmp_path):
    registry = _setup(tmp_path)
    batch = registry.release_batch(
        "COHORT-0",
        [{"candidate_id": "C-STRONG", "daily": STRONG}],
        CONTROL,
        capital=1000,
        economic_minimum=0.0001,
        b=2048,
    )
    assert batch["gates"]["status"] == "NOT_PROVIDED"
    assert batch["gates"]["all_pass"] is False
    assert batch["promotion_allowed"] is False
    assert batch["manifest"]["candidates"][0]["verdict"] in ("EDGE", "NO_EDGE", "INSUFFICIENT_EVIDENCE")
    assert batch["manifest"]["candidates"][0]["promoted"] is False
    assert registry.verify_release_batch(batch)["valid"] is True


# ------------------------------------------------------- multi-candidate ----
def test_multi_candidate_synthetic_cohort_release(tmp_path):
    candidates = [
        {"candidate_id": "C-STRONG", "daily": STRONG},
        {"candidate_id": "C-NULL", "daily": NULL},
        {"candidate_id": "C-INVALID", "daily": INVALID},
    ]
    keys = [_run_key(item["candidate_id"], item["daily"]) for item in candidates]
    registry = _setup(tmp_path, candidate_keys=keys, index=0)
    code_hash = registry._cohort_row("COHORT-0")[10]
    gate_evidence = _gate_evidence(code_hash)
    manifest = _manifest(registry, "COHORT-0", [item["candidate_id"] for item in candidates])

    # No per-candidate outcome is visible before the one batch release.
    assert _batch_count(registry) == 0
    assert registry.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0
    with pytest.raises(SealedError):
        registry.export_release_manifest("COHORT-0")

    batch = registry.release_batch(
        "COHORT-0",
        candidates,
        CONTROL,
        capital=1000,
        economic_minimum=0.0001,
        b=2048,
        seed=0,
        power=0.8,
        gate_evidence=gate_evidence,
        candidate_run_keys=keys,
        data_manifest=manifest,
    )
    exported = registry.export_release_manifest("COHORT-0")
    outcomes = {item["candidate_id"]: item for item in exported["candidates"]}

    assert _batch_count(registry) == 1
    assert exported["family_size"] == 3
    assert exported["invalid_in_family"] == 1
    assert set(outcomes) == {"C-STRONG", "C-NULL", "C-INVALID"}
    assert outcomes["C-INVALID"]["valid"] is False
    assert outcomes["C-INVALID"]["verdict"] == "INVALID"
    assert outcomes["C-INVALID"]["p_iut"] == 1.0
    assert outcomes["C-INVALID"]["p_holm"] == 1.0
    assert outcomes["C-INVALID"]["holm_significant"] is False
    assert outcomes["C-STRONG"]["valid"] is True
    assert outcomes["C-STRONG"]["p_holm"] >= outcomes["C-STRONG"]["p_iut"]
    assert '"daily":' not in json.dumps(exported)
    assert all("daily" not in candidate for candidate in exported["candidates"])
    assert exported["raw_inputs_released"] is False
    assert exported["gates"]["all_pass"] is True
    assert exported["promotion_allowed"] is True
    assert outcomes["C-STRONG"]["promoted"] is True
    assert exported["certification"] == {
        "synthetic_cohort": True,
        "market_result": False,
        "fresh_oos_confirmation": "EXTERNAL_BLOCKED",
        "g4_market_confirmation": "EXTERNAL_BLOCKED",
    }

    # Independent verification uses only the trusted public key and JCS bytes.
    assert batch["public_key"] == registry.public_key_hex()
    assert batch["public_key"] in registry.trusted_keys()
    _independent_verify(batch)
    assert registry.verify_release_batch(batch)["valid"] is True


# ----------------------------------------------------------- retry/dup ------
def test_exact_retry_returns_same_batch_and_spends_alpha_once(tmp_path):
    keys = [_run_key("C-STRONG", STRONG)]
    registry = _setup(tmp_path, candidate_keys=keys)
    code_hash = registry._cohort_row("COHORT-0")[10]
    manifest = _manifest(registry, "COHORT-0", ["C-STRONG"])
    kwargs = dict(
        capital=1000,
        economic_minimum=0.0001,
        b=64,
        gate_evidence=_gate_evidence(code_hash),
        candidate_run_keys=keys,
        data_manifest=manifest,
    )
    first = registry.release_batch(
        "COHORT-0", [{"candidate_id": "C-STRONG", "daily": STRONG}], CONTROL, **kwargs
    )
    retry = registry.release_batch(
        "COHORT-0", [{"candidate_id": "C-STRONG", "daily": STRONG}], CONTROL, **kwargs
    )
    assert retry["reused"] is True
    assert retry["batch_id"] == first["batch_id"]
    assert retry["signature"] == first["signature"]
    assert _batch_count(registry) == 1
    alpha = registry.db.execute(
        "SELECT alpha_spent_num,alpha_spent_den FROM consumption WHERE cohort_id='COHORT-0'"
    ).fetchone()
    assert alpha == (5, 200)
    assert len(_anchor_events(registry, "BatchRetry")) == 1

    # Same cohort, different inputs: refused and the stored batch is untouched.
    stored = registry.export_release_manifest("COHORT-0")
    with pytest.raises(SealedError) as exc:
        registry.release_batch(
            "COHORT-0",
            [{"candidate_id": "C-STRONG", "daily": [9.0, 9.0, 9.0, 9.0]}],
            CONTROL,
            **{key: value for key, value in kwargs.items() if key != "candidate_run_keys"},
        )
    assert exc.value.code == "PROTOCOL_VIOLATION"
    assert registry.export_release_manifest("COHORT-0")["batch_id"] == stored["batch_id"]
    assert _batch_count(registry) == 1


# ------------------------------------------------------- tampering/keys ------
def test_tampering_metric_decision_and_signature(tmp_path):
    registry = _setup(tmp_path)
    code_hash = registry._cohort_row("COHORT-0")[10]
    batch = registry.release_batch(
        "COHORT-0",
        [{"candidate_id": "C-STRONG", "daily": STRONG}],
        CONTROL,
        capital=1000,
        economic_minimum=0.0001,
        b=64,
        gate_evidence=_gate_evidence(code_hash),
        data_manifest=_manifest(registry, "COHORT-0", ["C-STRONG"]),
    )
    assert registry.verify_release_batch(batch)["valid"] is True

    metric = json.loads(json.dumps(batch))
    metric["manifest"]["candidates"][0]["p_iut"] = 0.0
    assert registry.verify_release_batch(metric)["valid"] is False

    verdict = json.loads(json.dumps(batch))
    verdict["manifest"]["candidates"][0]["verdict"] = "EDGE"
    assert registry.verify_release_batch(verdict)["valid"] is False

    signature = {**batch, "signature": "00" * 64}
    assert registry.verify_release_batch(signature)["reason"] == "INVALID_SIGNATURE"

    decision = registry.decide(
        "COHORT-0",
        STRONG,
        CONTROL,
        capital=1000,
        economic_minimum=0.0001,
        b=64,
    )
    assert registry.verify(decision) is True
    assert registry.verify({**decision, "verdict": "EDGE" if decision["verdict"] != "EDGE" else "NO_EDGE"}) is False
    other = SealedRegistry(str(tmp_path / "other"))
    assert registry.verify({**decision, "public_key": other.public_key_hex()}) is False


def test_unregistered_signing_key_is_rejected(tmp_path):
    registry = _setup(tmp_path)
    code_hash = registry._cohort_row("COHORT-0")[10]
    batch = registry.release_batch(
        "COHORT-0",
        [{"candidate_id": "C-STRONG", "daily": STRONG}],
        CONTROL,
        capital=1000,
        economic_minimum=0.0001,
        b=64,
        gate_evidence=_gate_evidence(code_hash),
        data_manifest=_manifest(registry, "COHORT-0", ["C-STRONG"]),
    )
    foreign = Ed25519PrivateKey.generate()
    foreign_public = (
        foreign.public_key()
        .public_bytes(
            encoding=__import__("cryptography.hazmat.primitives.serialization", fromlist=["Encoding"]).Encoding.Raw,
            format=__import__("cryptography.hazmat.primitives.serialization", fromlist=["PublicFormat"]).PublicFormat.Raw,
        )
        .hex()
    )
    withheld = registry.verify_release_batch(batch, trusted_keys={foreign_public})
    assert withheld["reason"] == "UNREGISTERED_KEY"

    body = {key: value for key, value in batch.items() if key not in ("signature", "public_key")}
    forged = {
        **body,
        "public_key": foreign_public,
        "signature": foreign.sign(canonical_bytes(body)).hex(),
    }
    assert registry.verify_release_batch(forged)["reason"] == "UNREGISTERED_KEY"

    # Registry trust is the root; a caller key that is also registered validates.
    registry.trust_key(foreign_public, "external")
    assert registry.verify_release_batch(forged)["valid"] is True


# ------------------------------------------------------ changed inputs ------
def test_changed_inputs_after_freeze_are_refused(tmp_path):
    keys = [_run_key("C-STRONG", STRONG)]
    registry = _setup(tmp_path, candidate_keys=keys)
    code_hash = registry._cohort_row("COHORT-0")[10]
    manifest = _manifest(registry, "COHORT-0", ["C-STRONG"])
    changed = [value + 1.0 for value in STRONG]

    with pytest.raises(SealedError) as exc:
        registry.release_batch(
            "COHORT-0",
            [{"candidate_id": "C-STRONG", "daily": changed}],
            CONTROL,
            capital=1000,
            economic_minimum=0.0001,
            b=64,
            gate_evidence=_gate_evidence(code_hash),
            candidate_run_keys=keys,
            data_manifest=manifest,
        )
    assert exc.value.code == "CHANGED_INPUTS"
    assert _batch_count(registry) == 0
    assert any(
        event.get("reason") == "CHANGED_INPUTS"
        for event in _anchor_events(registry, "ReleaseRefused")
    )

    # A declared key that is not in the frozen cohort is refused too.
    other_keys = [_run_key("C-OTHER", changed)]
    with pytest.raises(SealedError) as exc:
        registry.release_batch(
            "COHORT-0",
            [{"candidate_id": "C-OTHER", "daily": changed}],
            CONTROL,
            capital=1000,
            economic_minimum=0.0001,
            b=64,
            gate_evidence=_gate_evidence(code_hash),
            candidate_run_keys=other_keys,
            data_manifest=_manifest(registry, "COHORT-0", ["C-OTHER"]),
        )
    assert exc.value.code == "CHANGED_INPUTS"

    # After a valid release, raw inputs that differ from the signed manifest
    # are refused by verification.
    batch = registry.release_batch(
        "COHORT-0",
        [{"candidate_id": "C-STRONG", "daily": STRONG}],
        CONTROL,
        capital=1000,
        economic_minimum=0.0001,
        b=64,
        gate_evidence=_gate_evidence(code_hash),
        candidate_run_keys=keys,
        data_manifest=manifest,
    )
    assert registry.verify_release_batch(
        batch, raw_candidates=[{"candidate_id": "C-STRONG", "daily": changed}]
    )["reason"] == "CHANGED_INPUTS"
    assert registry.verify_release_batch(batch, expected_input_hash="0" * 64)["reason"] == "CHANGED_INPUTS"


# ------------------------------------------------------- contamination ------
def test_contamination_oos_development_mixed_is_refused(tmp_path):
    keys = [_run_key("C-STRONG", STRONG)]
    registry = _setup(tmp_path, name="sealed-overlap", candidate_keys=keys)
    code_hash = registry._cohort_row("COHORT-0")[10]
    overlap = _manifest(registry, "COHORT-0", ["C-STRONG"], contaminated=True)
    with pytest.raises(SealedError) as exc:
        registry.release_batch(
            "COHORT-0",
            [{"candidate_id": "C-STRONG", "daily": STRONG}],
            CONTROL,
            capital=1000,
            economic_minimum=0.0001,
            b=64,
            gate_evidence=_gate_evidence(code_hash),
            candidate_run_keys=keys,
            data_manifest=overlap,
        )
    assert exc.value.code == "CONTAMINATED_INPUT"
    assert _batch_count(registry) == 0
    assert any(
        event.get("reason") == "CONTAMINATED_INPUT"
        for event in _anchor_events(registry, "ReleaseRefused")
    )

    mixed = _setup(tmp_path, name="sealed-mixed", candidate_keys=keys)
    mixed_code = mixed._cohort_row("COHORT-0")[10]
    with pytest.raises(SealedError) as exc:
        mixed.release_batch(
            "COHORT-0",
            [{"candidate_id": "C-STRONG", "daily": STRONG}],
            CONTROL,
            capital=1000,
            economic_minimum=0.0001,
            b=64,
            gate_evidence=_gate_evidence(mixed_code),
            candidate_run_keys=keys,
            data_manifest=_manifest(
                mixed, "COHORT-0", ["C-STRONG"], development_source=True
            ),
        )
    assert exc.value.code == "CONTAMINATED_INPUT"

    clean = _setup(tmp_path, name="sealed-clean", candidate_keys=keys)
    clean_code = clean._cohort_row("COHORT-0")[10]
    batch = clean.release_batch(
        "COHORT-0",
        [{"candidate_id": "C-STRONG", "daily": STRONG}],
        CONTROL,
        capital=1000,
        economic_minimum=0.0001,
        b=64,
        gate_evidence=_gate_evidence(clean_code),
        candidate_run_keys=keys,
        data_manifest=_manifest(clean, "COHORT-0", ["C-STRONG"]),
    )
    assert batch["manifest"]["data_evidence"]["reason"] == "OOS_DEVELOPMENT_SEPARATION_VERIFIED"


# ------------------------------------------------------------ gates block ---
def test_failed_gate_blocks_promotion(tmp_path):
    keys = [_run_key("C-STRONG", STRONG), _run_key("C-INVALID", INVALID)]
    registry = _setup(tmp_path, candidate_keys=keys)
    code_hash = registry._cohort_row("COHORT-0")[10]
    batch = registry.release_batch(
        "COHORT-0",
        [
            {"candidate_id": "C-STRONG", "daily": STRONG},
            {"candidate_id": "C-INVALID", "daily": INVALID},
        ],
        CONTROL,
        capital=1000,
        economic_minimum=0.0001,
        b=2048,
        gate_evidence=_gate_evidence(code_hash, failed_gate="G2"),
        candidate_run_keys=keys,
        data_manifest=_manifest(registry, "COHORT-0", ["C-STRONG", "C-INVALID"]),
    )
    outcomes = {item["candidate_id"]: item for item in batch["manifest"]["candidates"]}
    assert outcomes["C-STRONG"]["verdict"] == "EDGE"
    assert outcomes["C-STRONG"]["promoted"] is False
    assert outcomes["C-INVALID"]["p_iut"] == 1.0
    assert batch["gates"]["first_block"] == "G2"
    assert batch["gates"]["all_pass"] is False
    assert batch["promotion_allowed"] is False
    assert batch["manifest"]["promotion_allowed"] is False
    assert registry.verify_release_batch(batch)["valid"] is True


# -------------------------------------------------------------- override ----
def test_authorized_override_is_a_separate_event(tmp_path):
    registry = _setup(tmp_path)
    code_hash = registry._cohort_row("COHORT-0")[10]
    batch = registry.release_batch(
        "COHORT-0",
        [{"candidate_id": "C-STRONG", "daily": STRONG}],
        CONTROL,
        capital=1000,
        economic_minimum=0.0001,
        b=64,
        gate_evidence=_gate_evidence(code_hash),
        data_manifest=_manifest(registry, "COHORT-0", ["C-STRONG"]),
    )
    stored_batch = json.dumps(registry.export_release_manifest("COHORT-0"), sort_keys=True)
    stored_body = registry.db.execute(
        "SELECT batch_json,signature FROM batches WHERE cohort_id='COHORT-0'"
    ).fetchone()

    override = registry.record_override(
        "COHORT-0",
        {"capital_authorization": True, "scope": "SYNTHETIC_ONLY"},
        authorized_by="synthetic-operator",
        reason="separate authorization event",
    )
    assert override["event_type"] == "OperatorOverride"
    assert override["anchor_hash"]
    assert len(_anchor_events(registry, "OperatorOverride")) == 1
    row = registry.db.execute(
        "SELECT override_json,authorized_by FROM overrides WHERE override_id=?",
        (override["override_id"],),
    ).fetchone()
    assert json.loads(row[0]) == {"capital_authorization": True, "scope": "SYNTHETIC_ONLY"}
    assert row[1] == "synthetic-operator"

    # The signed batch and its metrics are untouched by the override.
    assert json.dumps(registry.export_release_manifest("COHORT-0"), sort_keys=True) == stored_batch
    assert registry.db.execute(
        "SELECT batch_json,signature FROM batches WHERE cohort_id='COHORT-0'"
    ).fetchone() == stored_body
    assert registry.verify_release_batch(batch)["valid"] is True


def test_gate_requirements_match_architecture_fields():
    assert set(GATE_REQUIREMENTS) == {"G0", "G1", "G2", "G3", "G4"}
    assert set(GATE_REQUIREMENTS["G0"]) == {
        "preregistration.valid",
        "capital.minimum_order_feasible",
        "quality.required_coverage",
    }
    assert set(GATE_REQUIREMENTS["G1"]) == {
        "critical_fixtures.failed",
        "accounting.unexplained_residual_coeff",
        "lineage.valid",
    }
    assert set(GATE_REQUIREMENTS["G2"]) == {
        "walkforward.protocol_valid",
        "development.economic_effect_supported",
        "controls.superiority_supported",
        "parameter_stability.frozen_rule_pass",
    }
    assert set(GATE_REQUIREMENTS["G3"]) == {
        "capabilities.all_required_certified",
        "execution.uncertainty_budget_pass",
        "capacity.envelope_pass",
    }
    assert set(GATE_REQUIREMENTS["G4"]) == {
        "cohort.frozen_before_access",
        "window.valid_uncontaminated_spent_receipt",
        "independent_audit.pass",
        "statistics.calibration_gate_pass",
        "holm.conservative_p_rejected_null",
        "economic_effect.both_minima_pass",
    }
