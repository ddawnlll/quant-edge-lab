"""G0-G4 confirmation gates, batch release and synthetic cohort certification.

Every gate is an ``all_conditions`` AND. A missing field BLOCKs the gate; it is
never treated as a pass. The evaluator derives the fields it can observe
(lineage, frozen-before-access, spent receipt, Holm rejection and the economic
minima) itself; caller supplied evidence is only accepted for the external
audit/calibration fields and is labelled with its source. A synthetic cohort is
always labelled synthetic and is not a market result or a fresh OOS
confirmation.
"""

from __future__ import annotations

import random
import re
import resource
import time

from ..contract.canonical import canonical_bytes, hash_jcs
from .core import SealedError, SealedRegistry, verify_release_batch

_HEX64 = re.compile(r"^[0-9a-f]{64}$")

#: Required conditions per gate, copied from the architecture gate records.
GATE_REQUIREMENTS: dict[str, dict[str, object]] = {
    "G0": {
        "preregistration.valid": True,
        "capital.minimum_order_feasible": True,
        "quality.required_coverage": True,
    },
    "G1": {
        "critical_fixtures.failed": 0,
        "accounting.unexplained_residual_coeff": 0,
        "lineage.valid": True,
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
        "cohort.frozen_before_access": True,
        "window.valid_uncontaminated_spent_receipt": True,
        "independent_audit.pass": True,
        "statistics.calibration_gate_pass": True,
        "holm.conservative_p_rejected_null": True,
        "economic_effect.both_minima_pass": True,
    },
}

GATE_STAGES = {
    "G0": "ECONOMICS_DATA",
    "G1": "CORRECTNESS",
    "G2": "DEVELOPMENT",
    "G3": "FIDELITY",
    "G4": "CONFIRMATION",
}

GATE_DEPENDS: dict[str, tuple[str, ...]] = {
    "G0": (),
    "G1": ("G0",),
    "G2": ("G1",),
    "G3": ("G1", "G2"),
    "G4": ("G3",),
}

#: Conditions the trusted evaluator derives from the frozen registry/anchor.
#: A caller value that contradicts the derived value is a CONFLICT, never an
#: override of the scientific metric.
AUTHORITATIVE_GATE_FIELDS: dict[str, frozenset[str]] = {
    "G1": frozenset({"lineage.valid"}),
    "G4": frozenset(
        {
            "cohort.frozen_before_access",
            "window.valid_uncontaminated_spent_receipt",
            "holm.conservative_p_rejected_null",
            "economic_effect.both_minima_pass",
        }
    ),
}

#: Keys that would turn a gate document into a caller supplied decision.
FORBIDDEN_EVIDENCE_KEYS = frozenset(
    {
        "p",
        "p_value",
        "p_profit",
        "p_control",
        "p_iut",
        "p_holm",
        "adjusted_p",
        "verdict",
        "mean",
        "mean_daily_return",
        "ci_low",
        "ci_high",
        "holm_significant",
        "significance",
        "metrics",
        "decision",
        "code",
        "binary",
        "exec",
        "script",
        "callable",
        "module",
        "verifier",
    }
)


def _normalise(value):
    """Return a type-tagged value so bool and number never compare equal."""
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, (int, float)):
        return ("num", float(value))
    if isinstance(value, str):
        try:
            return ("num", float(value))
        except ValueError:
            return ("str", value)
    return ("other", value)


def _equal(actual, expected) -> bool:
    return _normalise(actual) == _normalise(expected)


def _derived_or_supplied(gate_id: str, metric: str, supplied: dict, derived: dict):
    if gate_id in derived and metric in derived[gate_id]:
        authoritative = derived[gate_id][metric]
        if metric in supplied and not _equal(supplied[metric], authoritative):
            return authoritative, "CONFLICT", supplied[metric]
        return authoritative, ("PASS" if _equal(authoritative, GATE_REQUIREMENTS[gate_id][metric]) else "FAIL"), authoritative
    if metric in supplied:
        value = supplied[metric]
        return value, ("PASS" if _equal(value, GATE_REQUIREMENTS[gate_id][metric]) else "FAIL"), value
    return None, "MISSING", None


def evaluate_gates(evidence: dict | None = None, derived: dict | None = None) -> dict:
    """Evaluate G0-G4 as an AND, failing closed on any missing field.

    ``derived`` carries values the trusted evaluator observed directly. Those
    values are authoritative: a different caller supplied value is recorded as
    a CONFLICT and blocks the gate.
    """
    derived = derived or {}
    supplied_gates = {}
    provided = evidence is not None
    if provided:
        if not isinstance(evidence, dict):
            raise SealedError("MISSING_GATE_FIELDS", "gate evidence must be an object")
        supplied_gates = evidence.get("gates", {})
        if not isinstance(supplied_gates, dict):
            raise SealedError("MISSING_GATE_FIELDS", "gate evidence must contain a gates object")

    results: dict[str, dict] = {}
    passed: dict[str, bool] = {}
    for gate_id, requirements in GATE_REQUIREMENTS.items():
        supplied = supplied_gates.get(gate_id, {})
        if not isinstance(supplied, dict):
            supplied = {}
        dependence_ok = all(passed.get(dep, False) for dep in GATE_DEPENDS[gate_id])
        conditions: dict[str, dict] = {}
        missing: list[str] = []
        failed: list[str] = []
        conflicts: list[str] = []
        for metric in requirements:
            actual, status, raw = _derived_or_supplied(gate_id, metric, supplied, derived)
            conditions[metric] = {
                "expected": requirements[metric],
                "actual": raw,
                "status": status,
                "authoritative": metric in AUTHORITATIVE_GATE_FIELDS.get(gate_id, ()),
            }
            if status == "MISSING":
                missing.append(metric)
            elif status == "CONFLICT":
                conflicts.append(metric)
                failed.append(metric)
            elif status == "FAIL":
                failed.append(metric)
        if not dependence_ok:
            status = "BLOCK"
            reason = "DEPENDENCY_BLOCKED"
        elif missing:
            status = "BLOCK"
            reason = "MISSING_GATE_FIELDS"
        elif failed:
            status = "BLOCK"
            reason = "CONDITION_FAILED"
        else:
            status = "PASS"
            reason = None
        passed[gate_id] = status == "PASS"
        results[gate_id] = {
            "stage": GATE_STAGES[gate_id],
            "depends_on": list(GATE_DEPENDS[gate_id]),
            "status": status,
            "reason": reason,
            "required": sorted(requirements),
            "missing": missing,
            "failed": failed,
            "conflicts": conflicts,
            "conditions": conditions,
        }

    first_block = next(
        (gate_id for gate_id in GATE_REQUIREMENTS if results[gate_id]["status"] != "PASS"),
        None,
    )
    all_pass = provided and first_block is None
    normalised = None
    if provided:
        normalised = {
            "source": evidence.get("source"),
            "synthetic": bool(evidence.get("synthetic", False)),
            "code_hash": evidence.get("code_hash"),
            "gates": {
                gate_id: {
                    metric: supplied_gates.get(gate_id, {}).get(metric)
                    for metric in GATE_REQUIREMENTS[gate_id]
                    if metric in supplied_gates.get(gate_id, {})
                }
                for gate_id in GATE_REQUIREMENTS
            },
        }
    return {
        "status": "PASS" if all_pass else ("NOT_PROVIDED" if not provided else "BLOCK"),
        "all_pass": all_pass,
        "evidence_status": "PROVIDED" if provided else "NOT_PROVIDED",
        "first_block": first_block,
        "gates": results,
        "synthetic": bool(evidence.get("synthetic", False)) if provided else False,
        "source": evidence.get("source") if provided else None,
        "code_hash": evidence.get("code_hash") if provided else None,
        "evidence_hash": hash_jcs(normalised) if provided else None,
        "gate_specs_hash": hash_jcs(GATE_REQUIREMENTS),
        "authoritative_fields": {
            gate_id: sorted(fields)
            for gate_id, fields in AUTHORITATIVE_GATE_FIELDS.items()
        },
    }


def validate_gate_evidence(
    evidence: dict | None, expected_code_hash: str | None = None
) -> dict:
    """Validate the signed-code and completeness fields of gate evidence.

    Explicitly supplied evidence is strict: missing gate fields, an unregistered
    code hash or any attempt to supply a decision metric raises before a batch
    is computed. ``None`` is allowed for legacy callers and yields a
    non-promotable release.
    """
    if evidence is None:
        return evaluate_gates(None)
    if not isinstance(evidence, dict):
        raise SealedError("MISSING_GATE_FIELDS", "gate evidence must be an object")
    missing = [key for key in ("source", "synthetic", "code_hash", "gates") if key not in evidence]
    if missing:
        raise SealedError("MISSING_GATE_FIELDS", f"gate evidence missing {sorted(missing)}")
    if not isinstance(evidence["source"], str) or not evidence["source"].strip():
        raise SealedError("MISSING_GATE_FIELDS", "gate evidence source is empty")
    if not isinstance(evidence["synthetic"], bool):
        raise SealedError("MISSING_GATE_FIELDS", "gate evidence synthetic flag must be boolean")
    code_hash = evidence["code_hash"]
    if not isinstance(code_hash, str) or not _HEX64.fullmatch(code_hash):
        raise SealedError("UNSIGNED_CODE", "gate evidence carries no signed code hash")
    if expected_code_hash is not None and code_hash != expected_code_hash:
        raise SealedError("UNSIGNED_CODE", "code hash does not match the frozen verifier")
    gates = evidence["gates"]
    if not isinstance(gates, dict):
        raise SealedError("MISSING_GATE_FIELDS", "gate evidence gates must be an object")
    missing_fields: list[str] = []
    unknown_fields: list[str] = []
    forbidden: list[str] = []
    for gate_id, requirements in GATE_REQUIREMENTS.items():
        supplied = gates.get(gate_id)
        if not isinstance(supplied, dict):
            missing_fields.append(gate_id)
            continue
        authoritative = AUTHORITATIVE_GATE_FIELDS.get(gate_id, frozenset())
        required = set(requirements) - set(authoritative)
        for metric in sorted(required - set(supplied)):
            missing_fields.append(f"{gate_id}:{metric}")
        for metric in sorted(set(supplied) & set(authoritative)):
            # The evaluator derives these from the frozen registry, anchor and
            # computed statistics. Accepting a caller value would be a forged
            # scientific result, even when it happens to match.
            forbidden.append(f"{gate_id}:{metric}")
        for metric in sorted(set(supplied) - set(requirements)):
            if metric in FORBIDDEN_EVIDENCE_KEYS or metric.split(".")[-1] in FORBIDDEN_EVIDENCE_KEYS:
                forbidden.append(f"{gate_id}:{metric}")
            else:
                unknown_fields.append(f"{gate_id}:{metric}")
    if forbidden:
        raise SealedError("CALLER_METRICS_REJECTED", f"forbidden gate fields {sorted(forbidden)}")
    if unknown_fields:
        raise SealedError("MISSING_GATE_FIELDS", f"unknown gate fields {sorted(unknown_fields)}")
    if missing_fields:
        raise SealedError("MISSING_GATE_FIELDS", f"missing gate fields {sorted(missing_fields)}")
    return evaluate_gates(evidence)


def gate_result_is_complete(result: dict | None) -> bool:
    """Structural check used on verification; missing fields never pass."""
    if not isinstance(result, dict):
        return False
    gates = result.get("gates")
    if not isinstance(gates, dict):
        return False
    if result.get("gate_specs_hash") != hash_jcs(GATE_REQUIREMENTS):
        return False
    for gate_id, requirements in GATE_REQUIREMENTS.items():
        entry = gates.get(gate_id)
        if not isinstance(entry, dict):
            return False
        if entry.get("status") not in ("PASS", "BLOCK"):
            return False
        conditions = entry.get("conditions")
        if not isinstance(conditions, dict) or set(conditions) != set(requirements):
            return False
    return True


def request_confirmation(registry: SealedRegistry, fresh_oos_available: bool, authorized: bool) -> dict:
    if not fresh_oos_available:
        return {"status": "WAITING_FRESH_OOS", "read": False, "reason": "no untouched window reserved"}
    if not authorized:
        return {"status": "WAITING_AUTHORIZED_COHORT", "read": False, "reason": "operator authorization missing"}
    raise SealedError("PROTOCOL_VIOLATION", "confirmation requires a frozen cohort commitment first")


def forward_calibration_ready(orders: int, fills: int, days: int, strata_ok: bool) -> dict:
    checks = {
        "orders_200": orders >= 200,
        "fills_50": fills >= 50,
        "days_10": days >= 10,
        "strata_adequate": strata_ok,
    }
    return {
        "status": "READY" if all(checks.values()) else "INSUFFICIENT_OBSERVATION",
        "checks": checks,
        "orders": orders,
        "fills": fills,
        "days": days,
    }


def build_export(decision: dict, capital_authorization: bool, signature_valid: bool) -> dict:
    if not decision.get("verdict") == "EDGE":
        return {"status": "REFUSED", "reason": "decision_not_edge", "orders": []}
    if not signature_valid:
        return {"status": "REFUSED", "reason": "INVALID_SIGNATURE", "orders": []}
    if not capital_authorization:
        return {"status": "REFUSED", "reason": "CAPITAL_UNAUTHORIZED", "orders": []}
    return {"status": "READY", "reason": None, "orders": [], "note": "scoped capital authorization still required for live"}


def release_batch_gate(
    registry: SealedRegistry,
    batch: dict,
    trusted_keys: set[str] | list[str] | dict | None = None,
    *,
    seen_batch_ids: set[str] | list[str] | None = None,
    expected_input_hash: str | None = None,
    raw_candidates: list[dict] | None = None,
) -> dict:
    """Verify a signed batch before release. Unknown keys and tampering fail closed."""
    result = verify_release_batch(
        batch,
        registry,
        trusted_keys,
        seen_batch_ids=seen_batch_ids,
        expected_input_hash=expected_input_hash,
        raw_candidates=raw_candidates,
    )
    if not result["valid"]:
        return {"status": "REFUSED", "reason": result["reason"], "batch_id": result["batch_id"]}
    return {
        "status": "READY",
        "reason": None,
        "batch_id": result["batch_id"],
        "manifest": result["manifest"],
    }


def redacted_release_manifest(registry: SealedRegistry, cohort_id: str) -> dict:
    """Return the stored redacted manifest. This never reads research artifacts."""
    return registry.export_release_manifest(cohort_id)


# ------------------------------------------------------------ synthetic demo
def _hash(label: str) -> str:
    return hash_jcs({"synthetic": label})


def _synthetic_series(mean: float, sd: float, seed: int, n: int = 60) -> list[float]:
    rng = random.Random(seed)
    return [mean + rng.gauss(0.0, sd) for _ in range(n)]


def _synthetic_gate_evidence(code_hash: str, *, failed_gate: str | None = None) -> dict:
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
    return {
        "source": "SYNTHETIC_EVALUATOR_CALIBRATION_VECTOR",
        "synthetic": True,
        "code_hash": code_hash,
        "gates": gates,
    }


def _data_manifest(
    cohort_id: str,
    commitment: str,
    split_hash: str,
    candidate_ids: list[str],
    *,
    contaminated: bool = False,
) -> dict:
    return {
        "cohort_id": cohort_id,
        "window_commitment": commitment,
        "split_hash": split_hash,
        "synthetic": True,
        "source": "SYNTHETIC_EVALUATOR_CALIBRATION_VECTOR",
        "oos_window": ["2026-01-01", "2026-06-30"],
        "development_windows": (
            [["2025-07-01", "2026-03-31"]] if contaminated else [["2024-01-01", "2024-12-31"]]
        ),
        "candidate_sources": {candidate_id: "SYNTHETIC_OOS" for candidate_id in candidate_ids},
        "control_source": "SYNTHETIC_OOS",
    }


def _prepare_cohort(
    registry: SealedRegistry,
    program_id: str,
    cohort_index: int,
    candidate_run_keys: list[str] | None = None,
) -> dict:
    candidate_keys = candidate_run_keys or [
        _hash(f"candidate-key-{program_id}-{index}") for index in range(3)
    ]
    verifier_hash = _hash(f"verifier-{program_id}")
    certificate_hash = _hash(f"certificate-{program_id}")
    frozen = registry.freeze_cohort(
        f"COHORT-{program_id}",
        program_id,
        cohort_index,
        candidate_keys,
        _hash(f"protocol-{program_id}"),
        _hash(f"split-{program_id}"),
        verifier_hash,
        [certificate_hash],
    )
    receipt = registry.prepare_read_receipt(f"COHORT-{program_id}")
    consumption = registry.spend_consumption(f"COHORT-{program_id}", acknowledged=True)
    return {
        "cohort_id": frozen["cohort_id"],
        "freeze": frozen,
        "receipt": receipt,
        "consumption": consumption,
        "verifier_hash": verifier_hash,
        "split_hash": _hash(f"split-{program_id}"),
        "certificate_hash": certificate_hash,
    }


def _independent_verify(batch: dict) -> dict:
    """Verify the signature using only the trusted public key and JCS bytes."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    body = {key: value for key, value in batch.items() if key not in ("signature", "public_key")}
    public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(batch["public_key"]))
    public_key.verify(bytes.fromhex(batch["signature"]), canonical_bytes(body))
    return {
        "valid": True,
        "public_key": batch["public_key"],
        "canonical_serialization": "RFC8785_JCS",
    }


def run_synthetic_release_demo(root: str) -> dict:
    """Freeze, consume, execute and release a labelled synthetic cohort.

    The result is a synthetic certification only. It is not a market result and
    it is not a fresh OOS confirmation; G4 market confirmation stays external.
    """
    started = time.perf_counter()
    registry = SealedRegistry(root)
    registry.trust_key(registry.public_key_hex(), "synthetic-evaluator")
    strong = _synthetic_series(2.5, 0.2, seed=7)
    null = _synthetic_series(0.0, 1.0, seed=11)
    invalid = [0.1]
    candidates = [
        {"candidate_id": "C-STRONG", "daily": strong},
        {"candidate_id": "C-NULL", "daily": null},
        {"candidate_id": "C-INVALID", "daily": invalid},
    ]
    candidate_keys = [
        hash_jcs({"candidate_id": item["candidate_id"], "daily": [float(v) for v in item["daily"]]})
        for item in candidates
    ]
    control = _synthetic_series(1.2, 0.2, seed=13)
    prepared = _prepare_cohort(
        registry, "PROG-SYNTH-RELEASE", 0, candidate_run_keys=candidate_keys
    )
    cohort_id = prepared["cohort_id"]

    # The frozen cohort must still carry the synthetic candidate keys. Re-freeze
    # is not possible, so this demo verifies the binding by replaying the hash
    # against the frozen commitment instead.
    manifest = _data_manifest(
        cohort_id, prepared["freeze"]["commitment"], prepared["split_hash"], [item["candidate_id"] for item in candidates]
    )
    gate_evidence = _synthetic_gate_evidence(prepared["verifier_hash"])

    before_release = {
        "batches": registry.db.execute("SELECT COUNT(*) FROM batches").fetchone()[0],
        "decisions": registry.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0],
        "manifest_available": False,
    }
    try:
        registry.export_release_manifest(cohort_id)
        before_release["manifest_available"] = True
    except SealedError:
        before_release["manifest_available"] = False

    batch = registry.release_batch(
        cohort_id,
        candidates,
        control,
        capital=1000,
        economic_minimum=0.0001,
        b=2048,
        seed=0,
        power=0.8,
        gate_evidence=gate_evidence,
        candidate_run_keys=candidate_keys,
        data_manifest=manifest,
    )
    retry = registry.release_batch(
        cohort_id,
        candidates,
        control,
        capital=1000,
        economic_minimum=0.0001,
        b=2048,
        seed=0,
        power=0.8,
        gate_evidence=gate_evidence,
        candidate_run_keys=candidate_keys,
        data_manifest=manifest,
    )
    independent = _independent_verify(batch)

    tampered = {**batch, "manifest": {**batch["manifest"], "candidates": [dict(item) for item in batch["manifest"]["candidates"]]}}
    tampered["manifest"]["candidates"][0]["p_iut"] = 1.0 - tampered["manifest"]["candidates"][0]["p_iut"]
    tampered_result = registry.verify_release_batch(tampered)
    duplicate_result = registry.verify_release_batch(batch, seen_batch_ids={batch["batch_id"]})
    changed_raw = registry.verify_release_batch(
        batch,
        raw_candidates=[
            {"candidate_id": "C-STRONG", "daily": [9.0, 9.0, 9.0]},
            {"candidate_id": "C-NULL", "daily": null},
            {"candidate_id": "C-INVALID", "daily": invalid},
        ],
    )
    override = registry.record_override(
        cohort_id,
        {"capital_authorization": True, "scope": "SYNTHETIC_ONLY"},
        authorized_by="synthetic-operator",
        reason="exercise a separate authorized override event",
    )
    exported = registry.export_release_manifest(cohort_id)

    # A contaminated row is refused and recorded as a separate anchor event.
    contaminated = SealedRegistry(root + "-contaminated")
    contaminated_candidates = [
        {"candidate_id": "C-STRONG", "daily": strong},
        {"candidate_id": "C-INVALID", "daily": invalid},
    ]
    contaminated_keys = [
        hash_jcs(
            {"candidate_id": item["candidate_id"], "daily": [float(v) for v in item["daily"]]}
        )
        for item in contaminated_candidates
    ]
    contaminated_prepared = _prepare_cohort(
        contaminated, "PROG-SYNTH-CONTAM", 0, candidate_run_keys=contaminated_keys
    )
    contamination_manifest = _data_manifest(
        contaminated_prepared["cohort_id"],
        contaminated_prepared["freeze"]["commitment"],
        contaminated_prepared["split_hash"],
        ["C-STRONG", "C-INVALID"],
        contaminated=True,
    )
    contamination_code = None
    try:
        contaminated.release_batch(
            contaminated_prepared["cohort_id"],
            contaminated_candidates,
            control,
            capital=1000,
            economic_minimum=0.0001,
            b=64,
            gate_evidence=_synthetic_gate_evidence(contaminated_prepared["verifier_hash"]),
            candidate_run_keys=contaminated_keys,
            data_manifest=contamination_manifest,
        )
    except SealedError as exc:
        contamination_code = exc.code
    refused_events = [
        record["payload"]
        for record in contaminated.anchor.records()
        if record["payload"].get("type") == "ReleaseRefused"
    ]

    # A failed gate blocks promotion even for a statistically strong candidate.
    blocked = SealedRegistry(root + "-blocked")
    blocked_candidates = [
        {"candidate_id": "C-STRONG", "daily": strong},
        {"candidate_id": "C-INVALID", "daily": invalid},
    ]
    blocked_keys = [
        hash_jcs(
            {"candidate_id": item["candidate_id"], "daily": [float(v) for v in item["daily"]]}
        )
        for item in blocked_candidates
    ]
    blocked_prepared = _prepare_cohort(
        blocked, "PROG-SYNTH-BLOCKED", 0, candidate_run_keys=blocked_keys
    )
    blocked_manifest = _data_manifest(
        blocked_prepared["cohort_id"],
        blocked_prepared["freeze"]["commitment"],
        blocked_prepared["split_hash"],
        ["C-STRONG", "C-INVALID"],
    )
    blocked_batch = blocked.release_batch(
        blocked_prepared["cohort_id"],
        blocked_candidates,
        control,
        capital=1000,
        economic_minimum=0.0001,
        b=2048,
        gate_evidence=_synthetic_gate_evidence(blocked_prepared["verifier_hash"], failed_gate="G2"),
        candidate_run_keys=blocked_keys,
        data_manifest=blocked_manifest,
    )

    alpha_spent = registry.db.execute(
        "SELECT alpha_spent_num,alpha_spent_den FROM consumption WHERE cohort_id=?", (cohort_id,)
    ).fetchone()
    outcomes = {item["candidate_id"]: item for item in exported["candidates"]}
    checks = {
        "pre_access_receipt_before_consumption": prepared["receipt"]["receipt_hash"]
        == prepared["consumption"]["receipt_hash"],
        "no_batch_before_release": before_release["batches"] == 0,
        "no_decision_before_release": before_release["decisions"] == 0,
        "no_manifest_before_release": before_release["manifest_available"] is False,
        "one_signed_batch": registry.db.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 1,
        "exact_retry_same_batch": retry.get("reused") is True
        and retry["batch_id"] == batch["batch_id"],
        "alpha_spent_once": (alpha_spent[0], alpha_spent[1]) == (5, 200),
        "independent_signature_valid": independent["valid"],
        "tampered_batch_refused": tampered_result["valid"] is False,
        "duplicate_release_refused": duplicate_result["reason"] == "DUPLICATE_RELEASE",
        "changed_inputs_refused": changed_raw["reason"] == "CHANGED_INPUTS",
        "invalid_candidate_in_family": outcomes["C-INVALID"]["valid"] is False
        and outcomes["C-INVALID"]["p_iut"] == 1.0
        and exported["family_size"] == 3,
        "all_outcomes_in_manifest": set(outcomes) == {"C-STRONG", "C-NULL", "C-INVALID"},
        "raw_inputs_redacted": exported["raw_inputs_released"] is False,
        "override_recorded_separately": override["event_type"] == "OperatorOverride",
        "contamination_refused": contamination_code == "CONTAMINATED_INPUT",
        "contamination_event_recorded": any(
            event.get("reason") == "CONTAMINATED_INPUT" for event in refused_events
        ),
        "failed_gate_blocks_promotion": blocked_batch["promotion_allowed"] is False
        and all(item["promoted"] is False for item in blocked_batch["manifest"]["candidates"]),
        "synthetic_labelled": exported["certification"]["synthetic_cohort"] is True
        and exported["certification"]["market_result"] is False,
    }
    return {
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "cohort": prepared,
        "candidate_run_keys": candidate_keys,
        "gate_evidence": gate_evidence,
        "data_manifest": manifest,
        "before_release": before_release,
        "batch": {
            "batch_id": batch["batch_id"],
            "input_hash": batch["input_hash"],
            "scope_hash": batch["scope_hash"],
            "evidence_hash": batch["evidence_hash"],
            "signature": batch["signature"],
            "public_key": batch["public_key"],
            "promotion_allowed": batch["promotion_allowed"],
            "reused_on_retry": retry.get("reused", False),
            "gates": {
                "status": batch["gates"]["status"],
                "first_block": batch["gates"]["first_block"],
            },
        },
        "manifest": exported,
        "independent_verification": independent,
        "negative_cases": {
            "tampered_batch": tampered_result,
            "duplicate_release": duplicate_result,
            "changed_inputs": changed_raw,
            "contamination": {"code": contamination_code, "events": refused_events},
            "failed_gate_batch": {
                "promotion_allowed": blocked_batch["promotion_allowed"],
                "first_block": blocked_batch["gates"]["first_block"],
                "candidates": [
                    {"candidate_id": item["candidate_id"], "verdict": item["verdict"], "promoted": item["promoted"]}
                    for item in blocked_batch["manifest"]["candidates"]
                ],
            },
        },
        "override": override,
        "certification": {
            "synthetic_cohort": True,
            "market_result": False,
            "fresh_oos_confirmation": "EXTERNAL_BLOCKED",
            "g4_market_confirmation": "EXTERNAL_BLOCKED",
        },
        "resource": {
            "wall_seconds": round(time.perf_counter() - started, 3),
            "maxrss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        },
    }
