"""Confirmation, forward and export gates. They fail closed."""

from __future__ import annotations

from .core import SealedError, SealedRegistry, verify_release_batch


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
