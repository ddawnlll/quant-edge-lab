"""Run-key and identity vectors (QEL serialization contract)."""

from __future__ import annotations

from .canonical import canonical_bytes, domain_hash, sha256_hex

RUN_DOMAIN = "QEL.RUN.1.1\x00"
LEDGER_DOMAIN = "QEL.LEDGER.1.1\x00"

RUN_KEY_OBJECT_KEYS = (
    "code_tree_hash",
    "dependency_lock_hash",
    "binary_hash",
    "dataset_manifest_hash",
    "strategy_hash",
    "venue_hash",
    "split_hash",
    "seed_protocol_hash",
    "environment_hash",
    "protocol_hash",
    "capital_profile_hash",
    "latency_hash",
    "control_spec_hash",
)


def run_key(material: dict) -> str:
    """Compute the run key from exactly the declared identity fields.

    attempt_id is intentionally not part of the identity. Missing or extra
    fields are rejected so a partial run cannot silently share an identity.
    """
    got = set(material)
    want = set(RUN_KEY_OBJECT_KEYS)
    if got != want:
        raise ValueError(f"run-key fields mismatch: missing={sorted(want-got)} extra={sorted(got-want)}")
    for k in RUN_KEY_OBJECT_KEYS:
        if not (isinstance(material[k], str) and len(material[k]) == 64):
            raise ValueError(f"run-key field {k} is not a 64-hex digest")
    return domain_hash(RUN_DOMAIN, {k: material[k] for k in RUN_KEY_OBJECT_KEYS})


def ledger_event_hash(event: dict, previous_hash: str) -> str:
    payload = {k: v for k, v in event.items() if k != "event_hash"}
    payload["previous_hash"] = previous_hash
    return domain_hash(LEDGER_DOMAIN, payload)


def deterministic_order_id(run_key_hex: str, strategy_id: str, decision_event_id: str, intent_index: int) -> str:
    h = sha256_hex(
        b"QEL.ORDER.1.1\x00"
        + canonical_bytes(
            {
                "run_key": run_key_hex,
                "strategy_id": strategy_id,
                "decision_event_id": decision_event_id,
                "intent_index": str(intent_index),
            }
        )
    )
    return "ORD-" + h.upper()


def event_id(domain: str, raw_hash: str, raw_offset: int, record_type: str, instrument_id: str, source_identity: str) -> str:
    return sha256_hex(
        domain.encode()
        + canonical_bytes(
            {
                "raw_hash": raw_hash,
                "raw_offset": str(raw_offset),
                "record_type": record_type,
                "instrument_id": instrument_id,
                "source_identity": source_identity,
            }
        )
    )
