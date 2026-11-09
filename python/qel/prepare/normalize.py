"""Normalize raw public captures into typed Arrow/Parquet datasets.

The normalizer is the provenance boundary between an immutable raw capture and
every downstream model.  Three identities are produced for each retained event:

``line_sha256``
    SHA-256 of the exact stored NDJSON line bytes, excluding the trailing LF.
    This is recomputable from the segment alone and therefore always verifies.

``raw_offset`` / ``raw_len``
    Absolute byte offset (``raw_offset_base + line_start``) and byte length of
    the stored line without the LF.  Declared collector offsets are checked
    against the real scan position.

``raw_sha256``
    The collector-recorded provenance hash.  Its meaning is explicit:
    ``RAW_FRAME_V1`` hashes the upstream HTTP response body, which is *not*
    stored in the NDJSON frame and therefore cannot be recomputed from the
    segment.  New/manifest-declared scopes ``RECORD_CANONICAL`` and
    ``PAYLOAD_CANONICAL`` are strict: a mismatch rejects the dataset.

Bitemporal rule resolution uses half-open valid-time and known-time ranges.
A rule whose ``known_at`` is later than the row decision time is never used;
the presence of such a rule without an earlier usable version rejects the
join (look-ahead guard).  Universe membership with no complete evidence is
never treated as complete: an explicit ``UNIVERSE_UNVERIFIED`` gate is emitted.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

import pyarrow as pa
import pyarrow.parquet as pq

from ..contract.canonical import canonical_bytes, hash_jcs, parse_decimal, rescale, sha256_hex
from ..contract.schema import ValidationError_, validate_def

PRICE_EXPONENT = 8
QTY_EXPONENT = 8
UINT64_MAX = 18446744073709551615

DATASET_ID = re.compile(r"^[A-Z][A-Z0-9_-]+$")
STREAM_ID = re.compile(r"^[A-Z][A-Z0-9_-]+$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
UINT_RE = re.compile(r"^(0|[1-9][0-9]*)$")
SYMBOL_RE = re.compile(r"^[A-Z0-9_-]+$")

EVENT_TYPES = ("Quote", "Trade", "Mark", "Funding")
COVERAGE_BY_EVENT = {"Quote": "QUOTE", "Trade": "TRADE", "Mark": "MARK", "Funding": "FUNDING"}

# Raw frame formats and the meaning of their raw_sha256 field.
RAW_FORMAT_HASH_SCOPE = {
    "RAW_FRAME_V1": "UPSTREAM_RESPONSE",
    "RAW_FRAME_V2": "RECORD_CANONICAL",
    "RAW_FRAME_LINE_V1": "RECORD_CANONICAL",
}
STRICT_HASH_SCOPES = {
    "RECORD_CANONICAL",
    "RECORD_CANONICAL_WITH_NEWLINE",
    "RECORD_JSON_SORTED",
    "RECORD_JSON_SORTED_WITH_NEWLINE",
    "RECORD_JSON_COMPACT",
    "RECORD_JSON_COMPACT_WITH_NEWLINE",
    "PAYLOAD_CANONICAL",
    "PAYLOAD_JSON_SORTED",
    "PAYLOAD_JSON_COMPACT",
    "STORED_LINE",
    "STORED_LINE_WITH_NEWLINE",
    "AUTO_STRICT",
}
HASH_SCOPE_ALIASES = {
    "RECORD": "RECORD_CANONICAL",
    "CANONICAL": "RECORD_CANONICAL",
    "LINE": "STORED_LINE",
    "LINE_NO_NEWLINE": "STORED_LINE",
    "LINE_WITH_NEWLINE": "STORED_LINE_WITH_NEWLINE",
    "PAYLOAD": "PAYLOAD_CANONICAL",
    "AUTO": "AUTO_STRICT",
    "UPSTREAM": "UPSTREAM_RESPONSE",
    "UPSTREAM_BODY": "UPSTREAM_RESPONSE",
}

# Runtime backend decision for this CPU.  DuckDB raises SIGILL (exit 132) with
# the pinned wheel on Intel Xeon E5-2680 v2; full evidence lives under
# evidence/labd/DUCKDB_* and evidence/labd/PATH_DECISION.json.
RUNTIME_PATH_DECISION = {
    "primary_backend": "PYARROW_PARQUET",
    "duckdb": {
        "status": "BLOCKED_BACKEND",
        "failure": "SIGILL_ILLEGAL_INSTRUCTION",
        "exit_code": 132,
        "cpu": "Intel(R) Xeon(R) CPU E5-2680 v2 @ 2.80GHz",
        "cpu_flags": "avx present; avx2 absent",
        "evidence": [
            "evidence/labd/PIT_DUCKDB_LIMITATION.json",
            "evidence/labd/DUCKDB_QUERY_EXIT.txt",
            "evidence/labd/DUCKDB_QUERY_STDERR.txt",
            "evidence/labd/DUCKDB_CPU.txt",
        ],
    },
    "semantic_parity_evidence": "evidence/labd/PIT_PARITY.json",
    "resource_evidence": "evidence/labd/PIT_RESOURCE.json",
    "decision_evidence": "evidence/labd/PIT_PATH_DECISION.json",
}


class NormalizeError(ValueError):
    """Base class for a rejected capture.  The message starts with a code."""


class RawHashMismatch(NormalizeError):
    """A recomputed raw identity does not match the declared one."""


class TimestampError(NormalizeError):
    """A timestamp is not a non-negative integer nanosecond value."""


class QuoteValidationError(NormalizeError):
    """An atomic top-of-book update is malformed."""


class DuplicateConflict(NormalizeError):
    """The same event identity appears twice with different content."""


class SequenceError(NormalizeError):
    """A snapshot/sequence boundary is invalid."""


class RuleError(NormalizeError):
    """Base class for bitemporal rule failures."""


class FutureKnownRule(RuleError):
    """Only a look-ahead rule covers the request; it cannot be used."""


class ConflictingRule(RuleError):
    """Two rules match with the same valid and known time but differ."""


class RuleNotFound(RuleError):
    """No rule matches and the caller required one."""


# ---------------------------------------------------------------------------
# scalar helpers


def _canonical_decimal(text: str) -> str:
    d = Decimal(text)
    if d == 0:
        return "0"
    out = format(d.normalize(), "f")
    if "." in out:
        out = out.rstrip("0").rstrip(".")
    return out


def _coeff(text: str, exponent: int, context: str) -> str:
    try:
        coeff, scale = parse_decimal(_canonical_decimal(text))
    except (InvalidOperation, ValueError) as exc:
        raise QuoteValidationError(f"INVALID_DECIMAL: {context}: {text!r}") from exc
    return str(rescale(coeff, scale, exponent))


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _uint(value: Any, field_name: str, context: str, maximum: int = UINT64_MAX) -> int:
    """Parse a canonical non-negative integer without accepting floats/negatives."""
    if isinstance(value, bool):
        raise TimestampError(f"INVALID_TIMESTAMP: {context}: {field_name} is boolean")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str):
        if not UINT_RE.fullmatch(value):
            raise TimestampError(f"INVALID_TIMESTAMP: {context}: {field_name}={value!r}")
        number = int(value)
    else:
        raise TimestampError(
            f"INVALID_TIMESTAMP: {context}: {field_name} must be integer ns, got {type(value).__name__}"
        )
    if number < 0 or number > maximum:
        raise TimestampError(f"INVALID_TIMESTAMP: {context}: {field_name}={number} outside [0,{maximum}]")
    return number


def _uint_field(value: Any, field_name: str, context: str, maximum: int = UINT64_MAX) -> int:
    try:
        return _uint(value, field_name, context, maximum)
    except TimestampError as exc:
        raise NormalizeError(str(exc).replace("INVALID_TIMESTAMP", "INVALID_INTEGER", 1)) from exc


def _decimal(value: Any, field_name: str, context: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise QuoteValidationError(f"INVALID_DECIMAL: {context}: {field_name} is not a scalar decimal")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise QuoteValidationError(f"INVALID_DECIMAL: {context}: {field_name}={value!r}") from exc
    if not number.is_finite():
        raise QuoteValidationError(f"INVALID_DECIMAL: {context}: {field_name} is not finite")
    return number


def _declared_hash(value: Any, context: str) -> str:
    if not isinstance(value, str) or not HASH_RE.fullmatch(value):
        raise RawHashMismatch(f"RAW_HASH_MISSING: {context}")
    return value


def _parse_json_int(value: Any, field_name: str, context: str) -> int:
    if isinstance(value, bool):
        raise NormalizeError(f"INVALID_INTEGER: {context}: {field_name} is boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"^-?(0|[1-9][0-9]*)$", value):
        return int(value)
    raise NormalizeError(f"INVALID_INTEGER: {context}: {field_name}={value!r}")


def _canonical_payload(payload: Any) -> bytes:
    try:
        return canonical_bytes(payload)
    except Exception:  # payloads with non-identity floats are still comparable
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _record_hash(rec: dict) -> str:
    body = {key: value for key, value in rec.items() if key != "raw_sha256"}
    return _sha256(canonical_bytes(body))


def _payload_hash(rec: dict) -> str:
    return _sha256(canonical_bytes(rec.get("payload")))


def _json_hash(value: Any, *, compact: bool) -> str:
    separators = (",", ":") if compact else None
    return _sha256(json.dumps(value, sort_keys=True, separators=separators).encode())


# ---------------------------------------------------------------------------
# raw scan


@dataclass(frozen=True)
class RawRecord:
    rec: dict
    line_start: int
    raw_offset: int
    raw_len: int
    line_sha256: str
    record_sha256: str
    hash_scope: str
    hash_verified: bool


def _hash_scope(rec: dict, manifest: dict) -> str:
    explicit = rec.get("raw_sha256_scope", rec.get("hash_scope"))
    if explicit is not None:
        if not isinstance(explicit, str):
            raise RawHashMismatch("RAW_HASH_SCOPE_INVALID: scope must be a string")
        scope = explicit.upper()
    else:
        declared = manifest.get("raw_hash_scope")
        if declared is not None:
            if not isinstance(declared, str):
                raise RawHashMismatch("RAW_HASH_SCOPE_INVALID: manifest scope must be a string")
            scope = declared.upper()
        else:
            scope = RAW_FORMAT_HASH_SCOPE.get(str(manifest.get("raw_format", "")), "AUTO_STRICT")
    scope = HASH_SCOPE_ALIASES.get(scope, scope)
    if scope not in STRICT_HASH_SCOPES | {"UPSTREAM_RESPONSE"}:
        raise RawHashMismatch(f"RAW_HASH_SCOPE_INVALID: {scope!r}")
    return scope


def _raw_offset_base(manifest: dict, segment: dict) -> int:
    raw = segment.get("raw_offset_base", manifest.get("raw_offset_base", 0))
    return _uint_field(raw, "raw_offset_base", "manifest")


def scan_segment(events: Path, manifest: dict, segment: dict) -> Iterable[RawRecord]:
    """Yield verified raw records with exact byte offsets and hashes."""
    data = events.read_bytes()
    expected = _declared_hash(segment.get("sha256"), "manifest segment sha256")
    actual = _sha256(data)
    if actual != expected:
        raise RawHashMismatch(f"RAW_SEGMENT_HASH_MISMATCH: file={actual} manifest={expected}")
    size = segment.get("size_bytes")
    if size is not None:
        declared_size = _uint_field(size, "size_bytes", "manifest")
        if declared_size != len(data):
            raise RawHashMismatch(
                f"RAW_SEGMENT_SIZE_MISMATCH: file={len(data)} manifest={declared_size}"
            )
    if not data:
        raise NormalizeError("EMPTY_CAPTURE: raw segment has no bytes")
    if not data.endswith(b"\n"):
        raise NormalizeError("TRUNCATED_TAIL: raw segment does not end with a newline")

    base = _raw_offset_base(manifest, segment)
    cursor = 0
    while cursor < len(data):
        newline = data.find(b"\n", cursor)
        if newline < 0:
            raise NormalizeError(f"TRUNCATED_TAIL: no newline after offset {cursor}")
        line = data[cursor:newline]
        if not line:
            raise NormalizeError(f"MALFORMED_LINE: empty line at offset {cursor}")
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            raise NormalizeError(f"MALFORMED_JSON: offset {cursor}: {exc.msg}") from exc
        if not isinstance(rec, dict):
            raise NormalizeError(f"MALFORMED_RECORD: offset {cursor}: record is not an object")

        if "raw_offset" in rec:
            declared_offset = _parse_json_int(rec["raw_offset"], "raw_offset", f"offset {cursor}")
            if declared_offset != cursor:
                raise RawHashMismatch(
                    f"RAW_OFFSET_MISMATCH: declared={declared_offset} actual={cursor}"
                )
        if "raw_len" in rec:
            declared_len = _parse_json_int(rec["raw_len"], "raw_len", f"offset {cursor}")
            if declared_len != len(line):
                raise RawHashMismatch(
                    f"RAW_LEN_MISMATCH: declared={declared_len} actual={len(line)}"
                )

        line_sha = _sha256(line)
        record_sha = _record_hash(rec)
        payload_sha = _payload_hash(rec)
        scope = _hash_scope(rec, manifest)
        declared = _declared_hash(rec.get("raw_sha256"), f"offset {cursor}")

        record_body = {key: value for key, value in rec.items() if key != "raw_sha256"}
        candidates = {
            "RECORD_CANONICAL": record_sha,
            "RECORD_CANONICAL_WITH_NEWLINE": _sha256(canonical_bytes(record_body) + b"\n"),
            "RECORD_JSON_SORTED": _json_hash(record_body, compact=False),
            "RECORD_JSON_SORTED_WITH_NEWLINE": _sha256(
                json.dumps(record_body, sort_keys=True).encode() + b"\n"
            ),
            "RECORD_JSON_COMPACT": _json_hash(record_body, compact=True),
            "RECORD_JSON_COMPACT_WITH_NEWLINE": _sha256(
                json.dumps(record_body, sort_keys=True, separators=(",", ":")).encode() + b"\n"
            ),
            "PAYLOAD_CANONICAL": payload_sha,
            "PAYLOAD_JSON_SORTED": _json_hash(rec.get("payload"), compact=False),
            "PAYLOAD_JSON_COMPACT": _json_hash(rec.get("payload"), compact=True),
            "STORED_LINE": line_sha,
            "STORED_LINE_WITH_NEWLINE": _sha256(line + b"\n"),
        }
        if scope == "AUTO_STRICT":
            matches = [name for name, digest in candidates.items() if digest == declared]
            if not matches:
                raise RawHashMismatch(
                    f"RAW_HASH_MISMATCH: offset {cursor}: declared={declared} "
                    f"candidates={sorted(candidates)}"
                )
            scope = sorted(matches)[0]
            verified = True
        elif scope == "UPSTREAM_RESPONSE":
            # The upstream HTTP body is not stored in the NDJSON frame.  If the
            # declared hash happens to match a stored candidate, record it.
            verified = declared in set(candidates.values())
        else:
            if declared != candidates[scope]:
                raise RawHashMismatch(
                    f"RAW_HASH_MISMATCH: offset {cursor}: declared={declared} "
                    f"recomputed={candidates[scope]}"
                )
            verified = True

        yield RawRecord(
            rec=rec,
            line_start=cursor,
            raw_offset=base + cursor,
            raw_len=len(line),
            line_sha256=line_sha,
            record_sha256=record_sha,
            hash_scope=scope,
            hash_verified=verified,
        )
        cursor = newline + 1

    if cursor != len(data):
        raise NormalizeError(f"TRUNCATED_TAIL: stopped at {cursor} of {len(data)} bytes")


# ---------------------------------------------------------------------------
# bitemporal rules


@dataclass(frozen=True)
class RuleVersion:
    kind: str
    keys: tuple[str, ...]
    valid_from: int
    valid_to: int
    known_at: int
    known_to: int | None
    payload: dict
    source_hash: str

    def fingerprint(self) -> str:
        return hash_jcs(
            {
                "kind": self.kind,
                "keys": list(self.keys),
                "valid_from": str(self.valid_from),
                "valid_to": str(self.valid_to),
                "known_at": str(self.known_at),
                "known_to": None if self.known_to is None else str(self.known_to),
                "payload": self.payload,
            }
        )


RULE_KINDS = ("INSTRUMENT", "FEE", "MARGIN", "TRADING_STATUS", "UNIVERSE_MEMBERSHIP")
STATUS_VALUES = {"TRADING", "HALTED", "DELISTED"}


def _rule_keys(obj: dict, kind: str) -> tuple[str, ...]:
    keys: list[str] = []
    for name in ("instrument_id", "symbol", "account_class", "universe_id", "currency_id"):
        value = obj.get(name)
        if isinstance(value, str) and value:
            keys.append(value)
    if kind == "FEE":
        keys.append("GLOBAL")
    return tuple(dict.fromkeys(keys)) or ("GLOBAL",)


def _range_pair(obj: dict, kind: str, context: str, *, known: bool) -> tuple[int, int | None]:
    if known:
        if "known_range" in obj:
            rng = obj["known_range"]
            start = _uint_field(rng.get("start_ns"), "known_range.start_ns", context)
            end = _uint_field(rng.get("end_ns"), "known_range.end_ns", context)
        else:
            start = _uint_field(obj.get("known_at_ns", obj.get("known_at")), "known_at", context)
            raw_end = obj.get("known_to_ns", obj.get("known_to"))
            end = None if raw_end is None else _uint_field(raw_end, "known_to", context)
        if end is not None and start >= end:
            raise RuleError(f"RULE_RANGE_INVALID: {context}: known range is empty")
        return start, end

    if "valid_range" in obj:
        rng = obj["valid_range"]
        start = _uint_field(rng.get("start_ns"), "valid_range.start_ns", context)
        end = _uint_field(rng.get("end_ns"), "valid_range.end_ns", context)
    else:
        start = _uint_field(obj.get("valid_from_ns", obj.get("valid_from")), "valid_from", context)
        raw_end = obj.get("valid_to_ns", obj.get("valid_to"))
        if raw_end is None:
            raise RuleError(f"RULE_RANGE_INVALID: {context}: valid_to is required")
        end = _uint_field(raw_end, "valid_to", context)
    if start >= end:
        raise RuleError(f"RULE_RANGE_INVALID: {context}: valid range is empty")
    return start, end


def _rule_version(kind: str, obj: dict, context: str) -> RuleVersion:
    if not isinstance(obj, dict):
        raise RuleError(f"RULE_INVALID: {context}: rule must be an object")
    valid_from, valid_to = _range_pair(obj, kind, context, known=False)
    known_at, known_to = _range_pair(obj, kind, context, known=True)
    source_hash = obj.get("source_hash")
    if not isinstance(source_hash, str) or not HASH_RE.fullmatch(source_hash):
        source_hash = hash_jcs(obj)
    payload = {key: value for key, value in obj.items() if key not in ("valid_range", "known_range")}
    return RuleVersion(
        kind=kind,
        keys=_rule_keys(obj, kind),
        valid_from=valid_from,
        valid_to=valid_to,
        known_at=known_at,
        known_to=known_to,
        payload=payload,
        source_hash=source_hash,
    )


class BitemporalRuleBook:
    """Half-open valid-time plus known-time rule index with look-ahead guards."""

    def __init__(self, document: dict):
        if not isinstance(document, dict):
            raise RuleError("RULES_INVALID: document must be an object")
        self.raw = document
        self.rules: dict[str, list[RuleVersion]] = {kind: [] for kind in RULE_KINDS}
        self.validation: dict[str, int] = {}

        for item in document.get("instrument_rules", []) or []:
            validate_def("InstrumentRule", item)  # raises ValidationError_
            self.rules["INSTRUMENT"].append(_rule_version("INSTRUMENT", item, "instrument_rules"))
        self.validation["InstrumentRule"] = len(self.rules["INSTRUMENT"])

        for item in document.get("fee_rules", []) or []:
            validate_def("FeeRule", item)
            self.rules["FEE"].append(_rule_version("FEE", item, "fee_rules"))
        self.validation["FeeRule"] = len(self.rules["FEE"])

        for item in document.get("margin_tiers", []) or []:
            if not isinstance(item, dict):
                raise RuleError("RULES_INVALID: margin_tiers entry must be an object")
            for tier in item.get("tiers", []) or []:
                validate_def("MarginTier", tier)
            envelope = dict(item)
            envelope.setdefault("source_hash", hash_jcs(item))
            self.rules["MARGIN"].append(_rule_version("MARGIN", envelope, "margin_tiers"))
        self.validation["MarginTier"] = len(self.rules["MARGIN"])

        for item in document.get("trading_status", []) or []:
            status = item.get("status") if isinstance(item, dict) else None
            if status not in STATUS_VALUES:
                raise RuleError(f"RULE_INVALID: trading_status has invalid status {status!r}")
            self.rules["TRADING_STATUS"].append(_rule_version("TRADING_STATUS", item, "trading_status"))
        self.validation["TradingStatus"] = len(self.rules["TRADING_STATUS"])

        for item in document.get("universe_membership", []) or []:
            if not isinstance(item, dict):
                raise RuleError("RULES_INVALID: universe_membership entry must be an object")
            self.rules["UNIVERSE_MEMBERSHIP"].append(
                _rule_version("UNIVERSE_MEMBERSHIP", item, "universe_membership")
            )
        self.validation["UniverseMembership"] = len(self.rules["UNIVERSE_MEMBERSHIP"])

        self.snapshot = document.get("universe_snapshot")

    def has_kind(self, kind: str) -> bool:
        return bool(self.rules.get(kind))

    def keys_for(self, kind: str) -> set[str]:
        out: set[str] = set()
        for rule in self.rules.get(kind, []):
            out.update(rule.keys)
        return out

    def resolve(
        self,
        kind: str,
        key: str,
        at_ns: int,
        decision_ns: int,
        *,
        required: bool = True,
    ) -> RuleVersion | None:
        """Resolve the latest correctly-known rule covering ``at_ns``.

        ``decision_ns`` is the knowable-at time (for capture rows, receive time).
        A candidate whose ``known_at`` is later than ``decision_ns`` is never
        returned.  If it is the only valid-time candidate the join is rejected
        as look-ahead rather than silently falling back.
        """
        at_ns = _uint_field(at_ns, "at_ns", "rule join")
        decision_ns = _uint_field(decision_ns, "decision_ns", "rule join")
        candidates = [
            rule
            for rule in self.rules.get(kind, [])
            if (key in rule.keys or "GLOBAL" in rule.keys)
            and rule.valid_from <= at_ns < rule.valid_to
        ]
        usable = [
            rule
            for rule in candidates
            if rule.known_at <= decision_ns
            and (rule.known_to is None or decision_ns < rule.known_to)
        ]
        if not usable:
            if candidates:
                raise FutureKnownRule(
                    f"RULE_FUTURE_KNOWN: {kind} key={key} at_ns={at_ns} decision_ns={decision_ns}"
                )
            if required:
                raise RuleNotFound(f"RULE_NOT_FOUND: {kind} key={key} at_ns={at_ns}")
            return None

        latest_known = max(rule.known_at for rule in usable)
        layer = [rule for rule in usable if rule.known_at == latest_known]
        fingerprints = {rule.fingerprint() for rule in layer}
        if len(fingerprints) > 1:
            raise ConflictingRule(
                f"RULE_CONFLICT: {kind} key={key} at_ns={at_ns} known_at={latest_known}"
            )
        # Also reject duplicate versions with identical valid+known identity but
        # different content anywhere in the usable set.
        grouped: dict[tuple[int, int, int], set[str]] = {}
        for rule in usable:
            identity = (rule.valid_from, rule.valid_to, rule.known_at)
            grouped.setdefault(identity, set()).add(rule.fingerprint())
        for identity, prints in grouped.items():
            if len(prints) > 1:
                raise ConflictingRule(f"RULE_CONFLICT: {kind} key={key} identity={identity}")
        return sorted(layer, key=lambda rule: rule.fingerprint())[0]

    def validate_rows(self, rows: list[dict], decision_offset_ns: int = 0) -> dict:
        """Join row valid time to row decision time and reject look-ahead rules."""
        joins = {kind: 0 for kind in RULE_KINDS}
        active = [kind for kind in ("INSTRUMENT", "FEE", "MARGIN", "TRADING_STATUS") if self.has_kind(kind)]
        for row in rows:
            key = row["symbol"]
            at_ns = int(row["ts_ns"])
            decision_ns = int(row["recv_ns"]) + decision_offset_ns
            for kind in active:
                rule = self.resolve(kind, key, at_ns, decision_ns, required=False)
                if rule is not None:
                    joins[kind] += 1
        return joins

    def universe_gate(self, symbols: list[str], decision_ns: int) -> tuple[bool, dict]:
        """Return (verified, gate).  Absent evidence is explicit, never assumed."""
        gate = {
            "gate_id": "GATE-UNIVERSE",
            "code": "UNIVERSE_UNVERIFIED",
            "status": "BLOCKED",
            "severity": "BLOCKING",
            "universe_verified": False,
            "blocks": ["CROSS_SECTIONAL_CLAIM", "GENERALIZED_UNIVERSE_CLAIM"],
            "symbols": sorted(set(symbols)),
            "reason": None,
        }
        if not self.rules["UNIVERSE_MEMBERSHIP"]:
            gate["reason"] = "NO_MEMBERSHIP_EVIDENCE"
            return False, gate
        if not isinstance(self.snapshot, dict) or self.snapshot.get("complete") is not True:
            gate["reason"] = "NO_COMPLETE_UNIVERSE_SNAPSHOT"
            return False, gate
        try:
            snapshot_known, _ = _range_pair(self.snapshot, "UNIVERSE_MEMBERSHIP", "universe_snapshot", known=True)
        except RuleError:
            gate["reason"] = "UNIVERSE_SNAPSHOT_KNOWN_TIME_INVALID"
            return False, gate
        if snapshot_known > decision_ns:
            gate["reason"] = "UNIVERSE_SNAPSHOT_FUTURE_KNOWN"
            return False, gate
        for symbol in sorted(set(symbols)):
            try:
                rule = self.resolve(
                    "UNIVERSE_MEMBERSHIP",
                    symbol,
                    decision_ns,
                    decision_ns,
                    required=True,
                )
            except (FutureKnownRule, RuleNotFound):
                gate["reason"] = f"NO_KNOWN_MEMBERSHIP:{symbol}"
                return False, gate
            if rule is not None and rule.payload.get("member") is not True:
                gate["reason"] = f"NOT_A_MEMBER:{symbol}"
                return False, gate
        gate.update(
            {
                "code": "UNIVERSE_VERIFIED",
                "status": "PASS",
                "severity": "INFO",
                "universe_verified": True,
                "reason": "COMPLETE_SNAPSHOT_AND_KNOWN_MEMBERSHIP",
                "snapshot_hash": hash_jcs(self.snapshot),
            }
        )
        return True, gate


def _load_rules(rules_path: str | None) -> tuple[BitemporalRuleBook | None, dict]:
    if not rules_path:
        return None, {"path": None, "sha256": None, "document": None}
    path = Path(rules_path)
    raw = path.read_bytes()
    document = json.loads(raw)
    try:
        book = BitemporalRuleBook(document)
    except ValidationError_ as exc:
        raise RuleError(f"RULES_SCHEMA_INVALID: {exc}") from exc
    return book, {"path": str(path), "sha256": _sha256(raw), "document": document}


# ---------------------------------------------------------------------------
# normalization


def _validate_common(rec: dict, context: str) -> tuple[int, int, dict, int]:
    kind = rec.get("record_type")
    if kind not in EVENT_TYPES:
        raise NormalizeError(f"UNKNOWN_EVENT_TYPE: {context}: {kind!r}")
    source_id = rec.get("source_id")
    symbol = rec.get("symbol")
    if not isinstance(source_id, str) or not source_id:
        raise NormalizeError(f"INVALID_SOURCE_ID: {context}")
    if not isinstance(symbol, str) or not SYMBOL_RE.fullmatch(symbol):
        raise NormalizeError(f"INVALID_SYMBOL: {context}: {symbol!r}")
    payload = rec.get("payload")
    if not isinstance(payload, dict):
        raise NormalizeError(f"INVALID_PAYLOAD: {context}: payload must be an object")
    ts_ns = _uint(rec.get("source_ts_ns"), "source_ts_ns", context)
    recv_ns = _uint(rec.get("ts_recv_ns"), "ts_recv_ns", context)
    return ts_ns, recv_ns, payload, kind


def _event_row(raw: RawRecord) -> tuple[dict, dict, dict]:
    rec = raw.rec
    context = f"offset {raw.raw_offset}"
    ts_ns, recv_ns, payload, kind = _validate_common(rec, context)
    row = {
        "event_type": kind,
        "ts_ns": ts_ns,
        "recv_ns": recv_ns,
        "price_i": None,
        "qty_i": None,
        "bid_i": None,
        "ask_i": None,
        "bid_qty_i": None,
        "ask_qty_i": None,
        "aggressor": None,
        "trade_id": None,
        "raw_sha256": rec["raw_sha256"],
        "line_sha256": raw.line_sha256,
        "raw_offset": raw.raw_offset,
        "raw_len": raw.raw_len,
        "raw_hash_scope": raw.hash_scope,
        "raw_hash_verified": raw.hash_verified,
        "source_id": rec["source_id"],
        "symbol": rec["symbol"],
    }
    meta: dict[str, Any] = {}

    if kind == "Quote":
        for name in ("bidPrice", "askPrice", "bidQty", "askQty"):
            if name not in payload:
                raise QuoteValidationError(f"QUOTE_INCOMPLETE: {context}: missing {name}")
        bid = _decimal(payload["bidPrice"], "bidPrice", context)
        ask = _decimal(payload["askPrice"], "askPrice", context)
        bid_qty = _decimal(payload["bidQty"], "bidQty", context)
        ask_qty = _decimal(payload["askQty"], "askQty", context)
        if bid <= 0:
            raise QuoteValidationError(f"QUOTE_BID_NONPOSITIVE: {context}: {bid}")
        if ask <= 0:
            raise QuoteValidationError(f"QUOTE_ASK_NONPOSITIVE: {context}: {ask}")
        if bid > ask:
            raise QuoteValidationError(f"QUOTE_BID_GT_ASK: {context}: bid={bid} ask={ask}")
        if bid_qty < 0 or ask_qty < 0:
            raise QuoteValidationError(f"QUOTE_NEGATIVE_SIZE: {context}")
        row.update(
            bid_i=_coeff(str(payload["bidPrice"]), PRICE_EXPONENT, f"{context} bidPrice"),
            ask_i=_coeff(str(payload["askPrice"]), PRICE_EXPONENT, f"{context} askPrice"),
            bid_qty_i=_coeff(str(payload["bidQty"]), QTY_EXPONENT, f"{context} bidQty"),
            ask_qty_i=_coeff(str(payload["askQty"]), QTY_EXPONENT, f"{context} askQty"),
        )
        if "lastUpdateId" in payload:
            meta["last_update_id"] = _parse_json_int(payload["lastUpdateId"], "lastUpdateId", context)
    elif kind == "Trade":
        for name in ("p", "q"):
            if name not in payload:
                raise QuoteValidationError(f"TRADE_INCOMPLETE: {context}: missing {name}")
        price = _decimal(payload["p"], "p", context)
        qty = _decimal(payload["q"], "q", context)
        if price <= 0:
            raise QuoteValidationError(f"TRADE_PRICE_NONPOSITIVE: {context}: {price}")
        if qty < 0:
            raise QuoteValidationError(f"TRADE_NEGATIVE_SIZE: {context}")
        trade_raw = payload.get("a", payload.get("trade_id"))
        if trade_raw is None:
            raise QuoteValidationError(f"TRADE_MISSING_ID: {context}")
        trade_id = str(_parse_json_int(trade_raw, "trade_id", context))
        if "m" in payload and not isinstance(payload["m"], bool):
            raise NormalizeError(f"INVALID_AGGRESSOR_FLAG: {context}")
        row.update(
            price_i=_coeff(str(payload["p"]), PRICE_EXPONENT, f"{context} p"),
            qty_i=_coeff(str(payload["q"]), QTY_EXPONENT, f"{context} q"),
            aggressor="SELL" if payload.get("m") else "BUY",
            trade_id=trade_id,
        )
    elif kind == "Mark":
        if "markPrice" not in payload:
            raise QuoteValidationError(f"MARK_INCOMPLETE: {context}")
        price = _decimal(payload["markPrice"], "markPrice", context)
        if price <= 0:
            raise QuoteValidationError(f"MARK_PRICE_NONPOSITIVE: {context}")
        row.update(price_i=_coeff(str(payload["markPrice"]), PRICE_EXPONENT, f"{context} markPrice"))
    else:  # Funding
        if "fundingRate" not in payload:
            raise QuoteValidationError(f"FUNDING_INCOMPLETE: {context}")
        _decimal(payload["fundingRate"], "fundingRate", context)
        row.update(price_i=_coeff(str(payload["fundingRate"]), 8, f"{context} fundingRate"))

    identity = _event_identity(kind, row, payload, ts_ns)
    return row, meta, identity


def _event_identity(kind: str, row: dict, payload: dict, ts_ns: int) -> tuple:
    if kind == "Trade":
        return ("Trade", row["symbol"], row["trade_id"])
    if kind == "Quote":
        uid = payload.get("lastUpdateId")
        if uid is not None:
            return ("Quote", row["symbol"], str(uid))
        return ("Quote", row["symbol"], ts_ns, _canonical_payload(payload))
    if kind == "Mark":
        return ("Mark", row["symbol"], ts_ns)
    return ("Funding", row["symbol"], ts_ns, payload.get("fundingTime"))


def _sequence_check(rec: dict, kind: str, symbol: str, context: str, states: dict) -> None:
    if "snapshot" in rec and not isinstance(rec["snapshot"], bool):
        raise SequenceError(f"SNAPSHOT_INVALID: {context}: snapshot must be boolean")
    if "snapshot" in rec and rec["snapshot"] is True and "sequence" not in rec:
        raise SequenceError(f"SNAPSHOT_MISSING_SEQUENCE: {context}")
    seq_raw = rec.get("sequence", rec.get("sequence_id"))
    if seq_raw is None:
        return
    seq = _uint_field(seq_raw, "sequence", context)
    stream = rec.get("stream_id", rec.get("stream", "DEFAULT"))
    if not isinstance(stream, str) or not stream:
        raise SequenceError(f"SEQUENCE_STREAM_INVALID: {context}")
    state = states.setdefault(
        (kind, rec.get("source_id"), symbol, stream),
        {"last_seq": None, "epoch": 0},
    )
    if rec.get("snapshot") is True:
        state["epoch"] += 1
        state["last_seq"] = seq
        return
    last = state["last_seq"]
    if last is not None:
        if seq < last:
            raise SequenceError(f"SEQUENCE_REGRESSION: {context}: {seq} < {last}")
        if seq == last:
            raise SequenceError(f"SEQUENCE_CONFLICT: {context}: duplicate sequence {seq}")
    state["last_seq"] = seq


def _gap_details(manifest: dict, fallback_end: int) -> list[dict]:
    gaps = []
    for index, event in enumerate(manifest.get("loss_events", []) or [], start=1):
        if not isinstance(event, dict):
            raise NormalizeError(f"GAP_INVALID: loss_events[{index}] must be an object")
        start = _uint_field(event.get("start_ns"), "start_ns", f"gap {index}")
        raw_end = event.get("end_ns")
        end = fallback_end if raw_end is None else _uint_field(raw_end, "end_ns", f"gap {index}")
        if start > end:
            raise NormalizeError(f"GAP_INVALID: gap {index} start > end")
        gaps.append(
            {
                "gap_id": str(event.get("gap_id", f"GAP-{index}")),
                "start_ns": str(start),
                "end_ns": str(end),
                "reason": str(event.get("reason", "UNKNOWN")),
                "last_good_seq": None if event.get("last_good_seq") is None else str(event["last_good_seq"]),
                "next_seen_seq": None if event.get("next_seen_seq") is None else str(event["next_seen_seq"]),
            }
        )
    return gaps


def _stream_id(source_id: str, symbol: str) -> str:
    candidate = re.sub(r"[^A-Z0-9_-]", "_", f"{source_id}_{symbol}_QUOTE".upper())
    if not candidate[0].isalpha():
        candidate = "S_" + candidate
    return candidate if STREAM_ID.fullmatch(candidate) else "QEL_QUOTE_STREAM"


def normalize_capture(
    events_path: str,
    manifest_path: str,
    out_root: str,
    dataset_id: str | None = None,
    rules_path: str | None = None,
    decision_ns: int | None = None,
    path_decision: dict | None = None,
) -> dict:
    """Normalize one raw capture segment and publish a certified dataset.

    Parameters are intentionally narrow: the raw segment and its manifest are
    the only required inputs.  ``rules_path`` enables bitemporal joins;
    ``decision_ns`` overrides the dataset decision time (default: latest
    receive timestamp).  Any rejection raises ``NormalizeError`` (or subclass)
    and no dataset is published.
    """
    events = Path(events_path)
    manifest_raw = Path(manifest_path).read_bytes()
    manifest = json.loads(manifest_raw)
    segments = manifest.get("segments") or []
    if len(segments) != 1:
        raise NormalizeError(f"MANIFEST_SEGMENT_COUNT: expected 1 segment, got {len(segments)}")
    segment = segments[0]
    if not isinstance(segment, dict):
        raise NormalizeError("MANIFEST_SEGMENT_INVALID: segment must be an object")

    dataset_id = dataset_id or ("DS-" + events.stem.upper().replace("-", "_")[:60])
    if not DATASET_ID.fullmatch(dataset_id):
        raise NormalizeError(f"INVALID_DATASET_ID: {dataset_id!r}")

    book, rules_meta = _load_rules(rules_path)

    rows: list[dict] = []
    seen: dict[tuple, bytes] = {}
    duplicates = 0
    duplicate_by_type: dict[str, int] = {kind: 0 for kind in EVENT_TYPES}
    conflicts = 0
    raw_hashes: set[str] = set()
    exchange_start = exchange_end = None
    receive_start = receive_end = None
    quote_uid_regressions = 0
    last_quote_uid: dict[str, int] = {}
    anchors: dict[str, dict] = {}
    sequence_states: dict = {}
    hash_verified_rows = 0
    hash_unverified_rows = 0
    scopes: set[str] = set()

    for raw in scan_segment(events, manifest, segment):
        rec = raw.rec
        context = f"offset {raw.raw_offset}"
        row, meta, identity = _event_row(raw)
        kind = row["event_type"]

        # Deterministic duplicate policy: keep first occurrence, drop identical
        # repeats, reject conflicting content for the same event identity.
        previous = seen.get(identity)
        if previous is not None:
            current = _canonical_payload(rec["payload"])
            if previous != current:
                conflicts += 1
                raise DuplicateConflict(
                    f"DUPLICATE_CONFLICT: {kind} identity={identity!r} at {context}"
                )
            duplicates += 1
            duplicate_by_type[kind] += 1
            continue
        seen[identity] = _canonical_payload(rec["payload"])

        _sequence_check(rec, kind, row["symbol"], context, sequence_states)

        if kind == "Quote" and "last_update_id" in meta:
            symbol = row["symbol"]
            uid = meta["last_update_id"]
            if symbol in last_quote_uid and uid < last_quote_uid[symbol]:
                quote_uid_regressions += 1
            last_quote_uid[symbol] = uid
            stream = _stream_id(row["source_id"], symbol)
            anchors[stream] = {
                "stream_id": stream,
                "last_update_id": str(uid),
                "raw_hash": rec["raw_sha256"],
            }

        ts_ns = row["ts_ns"]
        recv_ns = row["recv_ns"]
        exchange_start = ts_ns if exchange_start is None else min(exchange_start, ts_ns)
        exchange_end = ts_ns if exchange_end is None else max(exchange_end, ts_ns)
        receive_start = recv_ns if receive_start is None else min(receive_start, recv_ns)
        receive_end = recv_ns if receive_end is None else max(receive_end, recv_ns)

        raw_hashes.add(rec["raw_sha256"])
        scopes.add(raw.hash_scope)
        if raw.hash_verified:
            hash_verified_rows += 1
        else:
            hash_unverified_rows += 1

        rows.append(row)

    if not rows:
        raise NormalizeError("EMPTY_DATASET: no records retained")

    exchange_range = {"start_ns": str(exchange_start), "end_ns": str(exchange_end)}
    receive_range = {"start_ns": str(receive_start), "end_ns": str(receive_end)}
    gaps = _gap_details(manifest, receive_end)
    gap_ranges = [{"start_ns": gap["start_ns"], "end_ns": gap["end_ns"]} for gap in gaps]

    dataset_decision = (
        _uint_field(decision_ns, "decision_ns", "dataset") if decision_ns is not None else receive_end
    )

    rule_joins = {kind: 0 for kind in RULE_KINDS}
    universe_verified = False
    universe_gate = {
        "gate_id": "GATE-UNIVERSE",
        "code": "UNIVERSE_UNVERIFIED",
        "status": "BLOCKED",
        "severity": "BLOCKING",
        "universe_verified": False,
        "blocks": ["CROSS_SECTIONAL_CLAIM", "GENERALIZED_UNIVERSE_CLAIM"],
        "symbols": sorted({row["symbol"] for row in rows}),
        "reason": "NO_MEMBERSHIP_EVIDENCE",
    }
    rule_report: dict[str, Any] = {
        "present": book is not None,
        "rules_path": rules_meta["path"],
        "rules_sha256": rules_meta["sha256"],
        "decision_ns": str(dataset_decision),
        "validation": {} if book is None else book.validation,
        "joins": rule_joins,
    }
    if book is not None:
        rule_joins.update(book.validate_rows(rows))
        for kind in ("INSTRUMENT", "FEE", "MARGIN", "TRADING_STATUS"):
            if not book.has_kind(kind):
                continue
            for symbol in sorted({row["symbol"] for row in rows}):
                book.resolve(kind, symbol, dataset_decision, dataset_decision, required=False)
        universe_verified, universe_gate = book.universe_gate(
            sorted({row["symbol"] for row in rows}), dataset_decision
        )

    coverage = sorted({COVERAGE_BY_EVENT[row["event_type"]] for row in rows})
    clock_uncertainty = "0"
    sessions = manifest.get("sessions") or []
    if sessions and isinstance(sessions[0], dict) and sessions[0].get("clock_uncertainty_ns") is not None:
        clock_uncertainty = str(
            _uint_field(sessions[0]["clock_uncertainty_ns"], "clock_uncertainty_ns", "manifest session")
        )
    license_id = manifest.get("license_id", "BINANCE_PUBLIC_DATA_TERMS")

    restricted_reasons = []
    if gap_ranges:
        restricted_reasons.append("GAPS_PRESENT")
    if not universe_verified:
        restricted_reasons.append("UNIVERSE_UNVERIFIED")
    if hash_unverified_rows:
        restricted_reasons.append("RAW_HASH_UPSTREAM_UNVERIFIABLE")
    if quote_uid_regressions:
        restricted_reasons.append("QUOTE_SNAPSHOT_OUT_OF_ORDER")

    quality = {
        "dataset_hash": "0" * 64,  # replaced after the partition is written
        "normalizer_hash": sha256_hex(Path(__file__).read_bytes()),
        "raw_hashes": sorted(raw_hashes),
        "partition_count": 1,
        "row_count": str(len(rows)),
        "atomic_message_count": str(len(rows)),
        "exchange_range": exchange_range,
        "receive_range": receive_range,
        # Knowledge/availability time is the receive point for this capture.
        "availability_range": dict(receive_range),
        "duplicates": duplicates,
        "conflicts": conflicts,
        "gap_ranges": gap_ranges,
        "clock_uncertainty_ns": clock_uncertainty,
        "coverage": coverage,
        "universe_verified": universe_verified,
        "quarantine_hashes": [],
        "snapshot_anchors": [anchors[key] for key in sorted(anchors)],
        "verdict": "RESTRICTED" if restricted_reasons else "VALID",
    }

    schema = pa.schema(
        [
            ("event_type", pa.string()),
            ("ts_ns", pa.uint64()),
            ("recv_ns", pa.uint64()),
            ("price_i", pa.string()),
            ("qty_i", pa.string()),
            ("bid_i", pa.string()),
            ("ask_i", pa.string()),
            ("bid_qty_i", pa.string()),
            ("ask_qty_i", pa.string()),
            ("aggressor", pa.string()),
            ("trade_id", pa.string()),
            ("raw_sha256", pa.string()),
            ("line_sha256", pa.string()),
            ("raw_offset", pa.int64()),
            ("raw_len", pa.int64()),
            ("raw_hash_scope", pa.string()),
            ("raw_hash_verified", pa.bool_()),
            ("source_id", pa.string()),
            ("symbol", pa.string()),
        ]
    )
    table = pa.Table.from_pylist(rows, schema=schema)
    out = Path(out_root) / dataset_id
    out.mkdir(parents=True, exist_ok=True)
    partition = out / "events.parquet"
    pq.write_table(table, partition, compression="zstd", row_group_size=65536)
    dataset_hash = _hash_file(partition)
    quality["dataset_hash"] = dataset_hash
    validate_def("QualityCertificate", quality)
    qpath = out / "quality.json"
    qpath.write_text(json.dumps(quality, indent=2, sort_keys=True) + "\n")

    partition_ref = {
        "sha256": dataset_hash,
        "schema_id": "urn:qel:dataset:events",
        "schema_version": "1.1.0",
        "size_bytes": str(partition.stat().st_size),
        "visibility": "DEVELOPMENT",
        "media_type": "application/vnd.apache.parquet",
    }
    quality_ref = {
        "sha256": _hash_file(qpath),
        "schema_id": "urn:qel:dataset:quality",
        "schema_version": "1.1.0",
        "size_bytes": str(qpath.stat().st_size),
        "visibility": "DEVELOPMENT",
        "media_type": "application/json",
    }
    universe_artifact = {
        "schema_version": "1.1.0",
        "symbols": [
            {"symbol": symbol, "verified_history": universe_verified}
            for symbol in sorted({row["symbol"] for row in rows})
        ],
        "universe_verified": universe_verified,
        "gate": universe_gate,
    }
    reference_artifact = {
        "instrument": sorted({row["symbol"] for row in rows}),
        "price_exponent": PRICE_EXPONENT,
        "qty_exponent": QTY_EXPONENT,
        "rules_sha256": rules_meta["sha256"],
        "rule_kinds": sorted(kind for kind in RULE_KINDS if book and book.has_kind(kind)),
    }
    availability_policy = {
        "known_at": "RECEIVE_ONLY",
        "availability_field": "recv_ns",
        "no_future_data": True,
        "bitemporal": True,
        "decision_ns": str(dataset_decision),
        "universe_verified": universe_verified,
    }
    dataset = {
        "schema_version": "1.1.0",
        "dataset_id": dataset_id,
        "partitions": [partition_ref],
        "quality_certificate": quality_ref,
        "universe_hash": hash_jcs(universe_artifact),
        "reference_hash": hash_jcs(reference_artifact),
        "time_range": dict(exchange_range),
        "availability_policy_hash": hash_jcs(availability_policy),
        "license_id": license_id,
        "redistribution": bool(manifest.get("redistribution", False)),
        "revision_of": None,
    }
    validate_def("DatasetManifest", dataset)
    dpath = out / "dataset.json"
    dpath.write_text(json.dumps(dataset, indent=2, sort_keys=True) + "\n")

    # Raw provenance index.  raw_offset_base is manifest-level; per-row
    # offset/length/hash are duplicated into the Parquet for row-local proof.
    raw_index = [
        {
            "raw_offset": str(row["raw_offset"]),
            "raw_len": str(row["raw_len"]),
            "line_sha256": row["line_sha256"],
            "raw_sha256": row["raw_sha256"],
            "raw_hash_verified": row["raw_hash_verified"],
        }
        for row in rows
    ]
    raw_index_digest = hash_jcs(raw_index)
    raw_manifest = {
        "schema_version": "1.1.0",
        "dataset_id": dataset_id,
        "raw_offset_base": str(_raw_offset_base(manifest, segment)),
        "segment_sha256": segment["sha256"],
        "segment_size_bytes": str(segment.get("size_bytes", events.stat().st_size)),
        "raw_format": manifest.get("raw_format"),
        "raw_hash_scope": sorted(scopes)[0] if len(scopes) == 1 else "MIXED",
        "raw_index_sha256": raw_index_digest,
        "row_count": str(len(rows)),
        "rows_with_raw_offset": str(len(rows)),
        "rows_with_line_sha256": str(len(rows)),
        "rows_with_verified_declared_hash": str(hash_verified_rows),
        "rows_with_unverifiable_upstream_hash": str(hash_unverified_rows),
        "input_hashes": {
            "events_sha256": _sha256(events.read_bytes()),
            "manifest_sha256": _sha256(manifest_raw),
            "rules_sha256": rules_meta["sha256"],
        },
    }
    raw_manifest_path = out / "raw_manifest.json"
    raw_manifest_path.write_text(json.dumps(raw_manifest, indent=2, sort_keys=True) + "\n")

    rules_artifact = {
        "schema_version": "1.1.0",
        "present": book is not None,
        "rule_bundle_hash": hash_jcs(rules_meta["document"]) if book is not None else None,
        "validation": {} if book is None else book.validation,
        "document": rules_meta["document"],
    }
    (out / "rules.json").write_text(json.dumps(rules_artifact, indent=2, sort_keys=True) + "\n")
    gate_path = out / "gates.json"
    gate_path.write_text(
        json.dumps({"dataset_id": dataset_id, "gates": [universe_gate]}, indent=2, sort_keys=True) + "\n"
    )

    ordered_digest = _sha256(
        "\n".join(f"{row['ts_ns']}:{row['raw_sha256']}" for row in rows).encode()
    )
    certificate = {
        "certificate_version": "1.1.0",
        "dataset_id": dataset_id,
        "dataset_manifest": dataset,
        "quality_certificate": quality,
        "artifact_hashes": {
            "dataset_manifest_sha256": hash_jcs(dataset),
            "quality_certificate_sha256": hash_jcs(quality),
            "raw_manifest_sha256": hash_jcs(raw_manifest),
            "partition_sha256": dataset_hash,
            "ordered_ts_raw_sha256": ordered_digest,
            "raw_hash_set_sha256": hash_jcs(
                sorted({row["line_sha256"] for row in rows})
            ),
            "upstream_raw_hash_set_sha256": hash_jcs(sorted(raw_hashes)),
        },
        "coverage": {
            "row_count": str(len(rows)),
            "event_types": coverage,
            "event_type_counts": {
                kind: sum(1 for row in rows if row["event_type"] == kind) for kind in EVENT_TYPES
            },
            "exchange_range": exchange_range,
            "receive_range": receive_range,
            "coverage_limits": [
                "SINGLE_INSTRUMENT_CAPTURE",
                "TOP_OF_BOOK_QUOTES_ONLY",
                "NO_OBSERVED_L2_DEPTH",
                "NO_QUEUE_POSITION",
                "NO_UNIVERSE_COMPLETENESS_WITHOUT_MEMBERSHIP_EVIDENCE",
            ],
        },
        "duplicates": {
            "identical_dropped": duplicates,
            "conflicting": conflicts,
            "by_event_type": duplicate_by_type,
            "policy": "FIRST_OCCURRENCE_KEPT_CONFLICT_REJECTED",
        },
        "gaps": gaps,
        "raw_provenance": raw_manifest,
        "rules": rule_report,
        "universe": {
            "universe_verified": universe_verified,
            "gate": universe_gate,
        },
        "gates": [universe_gate],
        "path_decision": path_decision or RUNTIME_PATH_DECISION,
        "input_hashes": raw_manifest["input_hashes"],
        "normalizer_hash": quality["normalizer_hash"],
        "restricted_reasons": restricted_reasons,
        "verdict": quality["verdict"],
        "claims": [
            "every retained row resolves to exact stored line bytes and byte offset",
            "identical duplicates are dropped deterministically; conflicts reject the dataset",
            "timestamps are validated non-negative integer nanoseconds",
        ],
        "non_claims": [
            "checksum alone does not certify completeness",
            "top-of-book quotes are not L2 depth",
            "single-instrument capture is not a complete cross-sectional universe",
        ],
    }
    # Embedded objects must satisfy the canonical schema defs.
    validate_def("DatasetManifest", certificate["dataset_manifest"])
    validate_def("QualityCertificate", certificate["quality_certificate"])
    cpath = out / "dataset_certificate.json"
    cpath.write_text(json.dumps(certificate, indent=2, sort_keys=True) + "\n")

    return {
        "dataset_id": dataset_id,
        "dataset_path": str(dpath),
        "partition_path": str(partition),
        "quality_path": str(qpath),
        "certificate_path": str(cpath),
        "certificate_sha256": _hash_file(cpath),
        "raw_manifest_path": str(raw_manifest_path),
        "gate_path": str(gate_path),
        "rows": len(rows),
        "duplicates": duplicates,
        "conflicts": conflicts,
        "coverage": coverage,
        "dataset_hash": dataset_hash,
        "ordered_ts_raw_sha256": ordered_digest,
        "raw_hash_scope": raw_manifest["raw_hash_scope"],
        "raw_hash_verified_rows": hash_verified_rows,
        "raw_hash_unverifiable_rows": hash_unverified_rows,
        "universe_verified": universe_verified,
        "gates": [universe_gate],
        "verdict": quality["verdict"],
    }
