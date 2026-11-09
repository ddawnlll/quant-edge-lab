"""Sealed evaluator: cohort freeze, pre-access consumption and signed decisions.

The private registry is a separate SQLite file with its own durable anchor log.
The anchor log lives outside the sealed registry root (``QEL_ANCHOR_ROOT`` or a
sibling directory) so that restoring, renaming or copying the registry cannot
rewind consumption. Full OS-owner separation is recorded as a deployment
blocker where distinct Unix principals are unavailable.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import re
import sqlite3
import stat
import time
from fractions import Fraction
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives import serialization

from ..contract.canonical import canonical_bytes, hash_jcs
from ..stats.core import classify, holm, iut, studentized_bootstrap_p

RESEARCH_ARTIFACT_DIRS = ("evidence", "labd", "native", "locks", "policy")

#: A candidate may only carry its identifier and raw authorized series. Every
#: decision metric is derived by the evaluator; a caller supplied metric is a
#: request to forge the scientific result.
CANDIDATE_ALLOWED_KEYS = frozenset({"candidate_id", "daily"})

FORBIDDEN_CALLER_KEYS = frozenset(
    {
        "p",
        "p_value",
        "p_profit",
        "p_control",
        "p_iut",
        "p_holm",
        "adjusted_p",
        "verdict",
        "scientific_verdict",
        "mean",
        "mean_daily_return",
        "ci_low",
        "ci_high",
        "holm_significant",
        "significance",
        "metrics",
        "decision",
        "promoted",
        "promotion_allowed",
        "code",
        "binary",
        "exec",
        "script",
        "callable",
        "module",
        "verifier",
    }
)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")

_CONTAMINATING_SOURCES = frozenset({"DEVELOPMENT", "VALIDATION", "TRAIN", "IN_SAMPLE"})
_OOS_SOURCES = frozenset({"OOS", "FRESH_OOS", "SYNTHETIC_OOS", "SEALED_OOS"})


def _is_hex64(value) -> bool:
    return isinstance(value, str) and bool(_HEX64.fullmatch(value))


def _reject_forbidden_keys(payload: dict, where: str) -> None:
    forbidden = sorted(
        key
        for key in payload
        if key in FORBIDDEN_CALLER_KEYS or key.split(".")[-1] in FORBIDDEN_CALLER_KEYS
    )
    if forbidden:
        raise SealedError(
            "CALLER_METRICS_REJECTED",
            f"{where} may not supply decision metrics {forbidden}",
        )


def _finite_series(values, field: str) -> list[float]:
    if not isinstance(values, (list, tuple)):
        raise SealedError("INVALID_DATA", f"{field} must be a list of numbers")
    out: list[float] = []
    for value in values:
        if isinstance(value, bool):
            raise SealedError("INVALID_DATA", f"{field} contains a boolean")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise SealedError("INVALID_DATA", f"{field} contains a non-number") from exc
        if not math.isfinite(number):
            raise SealedError("INVALID_DATA", f"{field} contains a non-finite value")
        out.append(number)
    return out


def _candidate_run_key(candidate: dict) -> str:
    """Deterministic frozen-input key for one candidate."""
    candidate_id = str(candidate.get("candidate_id", ""))
    daily = candidate.get("daily")
    try:
        values = [float(value) for value in daily]
    except (TypeError, ValueError):
        values = None
    return hash_jcs({"candidate_id": candidate_id, "daily": values})


class SealedError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def _now() -> str:
    return str(time.time_ns())


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class AnchorStore:
    """Append-only, fsynced hash chain outside the sealed registry root.

    Each record carries a monotonically increasing generation, the previous
    anchor hash, a payload hash, the cohort id, the alpha spend and the window
    commitment. ``acknowledge`` reads the record back from disk; callers that
    do not receive an acknowledgement must not release an experimental read.
    """

    def __init__(self, root: str, *, fail_ack: bool = False):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.path = self.root / "anchors.log"
        self.lock_path = self.root / ".anchor.lock"
        for path in (self.path, self.lock_path):
            if not path.exists():
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(fd)
            os.chmod(path, 0o600)
        self.fail_ack = fail_ack

    @contextlib.contextmanager
    def _exclusive(self):
        fd = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _load(self) -> list[dict]:
        if not self.path.exists():
            return []
        records: list[dict] = []
        for line in self.path.read_text().splitlines():
            if line.strip():
                records.append(json.loads(line))
        return records

    def head(self) -> dict | None:
        records = self._load()
        return records[-1] if records else None

    def generation(self) -> int:
        head = self.head()
        return int(head["generation"]) if head else 0

    def records(self) -> list[dict]:
        return self._load()

    @staticmethod
    def _chain_body(
        generation: int,
        prev_hash: str,
        payload: dict,
        payload_hash: str,
        cohort_id: str | None,
        alpha_num: int | None,
        alpha_den: int | None,
        window_commitment: str | None,
    ) -> dict:
        return {
            "generation": generation,
            "prev_hash": prev_hash,
            "payload": payload,
            "payload_hash": payload_hash,
            "cohort_id": cohort_id,
            "alpha_num": alpha_num,
            "alpha_den": alpha_den,
            "window_commitment": window_commitment,
        }

    def append(self, payload: dict) -> dict:
        with self._exclusive():
            records = self._load()
            previous = records[-1]["anchor_hash"] if records else "0" * 64
            generation = (int(records[-1]["generation"]) if records else 0) + 1
            body = self._chain_body(
                generation,
                previous,
                payload,
                hash_jcs(payload),
                payload.get("cohort_id"),
                payload.get("alpha_num"),
                payload.get("alpha_den"),
                payload.get("window_commitment"),
            )
            record = {**body, "anchor_hash": hash_jcs(body)}
            line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND)
            try:
                os.write(fd, line.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            _fsync_dir(self.root)
            return record

    def acknowledge(self, record: dict) -> dict | None:
        """Read the record back from disk. A missing/different record means no ack."""
        if self.fail_ack:
            return None
        reread = self._load()
        if not reread:
            return None
        last = reread[-1]
        if last.get("anchor_hash") != record.get("anchor_hash"):
            return None
        if int(last.get("generation", -1)) != int(record.get("generation", -2)):
            return None
        return last

    def verify_chain(self) -> dict:
        records = self._load()
        previous = "0" * 64
        errors: list[str] = []
        for index, record in enumerate(records):
            expected_generation = index + 1
            if int(record.get("generation", -1)) != expected_generation:
                errors.append(f"generation mismatch at {index}")
            if record.get("prev_hash") != previous:
                errors.append(f"prev_hash mismatch at {index}")
            payload = record.get("payload", {})
            if record.get("payload_hash") != hash_jcs(payload):
                errors.append(f"payload_hash mismatch at {index}")
            body = self._chain_body(
                int(record.get("generation", -1)),
                record.get("prev_hash", ""),
                payload,
                record.get("payload_hash", ""),
                record.get("cohort_id"),
                record.get("alpha_num"),
                record.get("alpha_den"),
                record.get("window_commitment"),
            )
            if record.get("anchor_hash") != hash_jcs(body):
                errors.append(f"anchor_hash mismatch at {index}")
            previous = record.get("anchor_hash", "")
        return {
            "valid": not errors,
            "errors": errors,
            "records": len(records),
            "generation": len(records),
            "head_hash": previous if records else None,
        }

    def find_by_commitment(self, commitment: str) -> list[dict]:
        return [
            record
            for record in self._load()
            if record.get("window_commitment") == commitment
        ]

    def latest_consumption(self, commitment: str) -> dict | None:
        for record in reversed(self._load()):
            if (
                record.get("window_commitment") == commitment
                and record.get("payload", {}).get("type") == "OOSConsumed"
            ):
                return record
        return None

    def find_window(self, program_id: str, cohort_index: int) -> list[dict]:
        return [
            record
            for record in self._load()
            if record.get("payload", {}).get("program_id") == program_id
            and record.get("payload", {}).get("cohort_index") == cohort_index
            and record.get("payload", {}).get("type") in ("CohortFrozen", "OOSConsumed")
        ]


class SealedRegistry:
    def __init__(self, root: str, *, anchor_root: str | None = None, anchor: AnchorStore | None = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.db_path = self.root / "sealed.sqlite"
        self.key_path = self.root / "evaluator.key"
        self.pending_path = self.root / "pending_consumption.json"
        configured = anchor_root or os.environ.get("QEL_ANCHOR_ROOT")
        self.anchor_root = (
            Path(configured) if configured else self.root.parent / (self.root.name + ".anchor")
        )
        self.anchor = anchor or AnchorStore(str(self.anchor_root))
        self.anchor_path = self.anchor.path
        self._assert_anchor_independent()
        self.db = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        os.chmod(self.db_path, 0o600)
        self._migrate()
        self._ensure_key()
        self.local_generation = int(self._meta("anchor_generation") or 0)
        head = self.anchor.head()
        stored_root = self._meta("registry_root")
        current_root = str(self.root.resolve())
        if stored_root is None:
            self._set_meta("registry_root", current_root)
            self.db.commit()
            self.root_mismatch = False
        else:
            self.root_mismatch = stored_root != current_root
        self.requires_reconcile = bool(
            (head and int(head["generation"]) > self.local_generation) or self.root_mismatch
        )

    # ------------------------------------------------------------ lifecycle
    def close(self) -> None:
        db = getattr(self, "db", None)
        if db is not None:
            db.close()
            self.db = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def _assert_anchor_independent(self) -> None:
        anchor_root = self.anchor.root.resolve()
        sealed_root = self.root.resolve()
        if anchor_root == sealed_root or anchor_root.is_relative_to(sealed_root):
            raise SealedError(
                "ISOLATION_VIOLATION",
                "anchor root must be outside the sealed registry root",
            )

    def _migrate(self) -> None:
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS cohorts (
              cohort_id TEXT PRIMARY KEY,
              program_id TEXT NOT NULL,
              cohort_index INTEGER NOT NULL,
              commitment TEXT NOT NULL,
              alpha_num INTEGER NOT NULL,
              alpha_den INTEGER NOT NULL,
              state TEXT NOT NULL,
              created_ns TEXT NOT NULL,
              candidate_run_keys TEXT NOT NULL DEFAULT '[]',
              protocol_hash TEXT NOT NULL DEFAULT '',
              split_hash TEXT NOT NULL DEFAULT '',
              verifier_binary_hash TEXT NOT NULL DEFAULT '',
              certificate_hashes TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS consumption (
              cohort_id TEXT PRIMARY KEY,
              receipt_hash TEXT NOT NULL,
              alpha_spent_num INTEGER NOT NULL,
              alpha_spent_den INTEGER NOT NULL,
              anchor_hash TEXT NOT NULL,
              created_ns TEXT NOT NULL,
              commitment TEXT NOT NULL DEFAULT '',
              request_id TEXT
            );
            CREATE TABLE IF NOT EXISTS decisions (
              cohort_id TEXT PRIMARY KEY,
              verdict TEXT NOT NULL,
              decision_json TEXT NOT NULL,
              signature TEXT NOT NULL,
              created_ns TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS trials (
              trial_key TEXT PRIMARY KEY,
              cohort_id TEXT NOT NULL,
              created_ns TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS leases (
              lease_id TEXT PRIMARY KEY,
              generation INTEGER NOT NULL,
              updated_ns TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS read_receipts (
              cohort_id TEXT PRIMARY KEY,
              commitment TEXT NOT NULL,
              receipt_hash TEXT NOT NULL,
              receipt_json TEXT NOT NULL,
              signature TEXT NOT NULL,
              created_ns TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS batches (
              batch_id TEXT PRIMARY KEY,
              cohort_id TEXT NOT NULL UNIQUE,
              input_hash TEXT NOT NULL,
              batch_json TEXT NOT NULL,
              signature TEXT NOT NULL,
              public_key TEXT NOT NULL,
              created_ns TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS trusted_keys (
              public_key TEXT PRIMARY KEY,
              label TEXT NOT NULL,
              added_ns TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS overrides (
              override_id TEXT PRIMARY KEY,
              cohort_id TEXT NOT NULL,
              override_json TEXT NOT NULL,
              authorized_by TEXT NOT NULL,
              reason TEXT NOT NULL,
              anchor_hash TEXT NOT NULL,
              created_ns TEXT NOT NULL
            );
            """
        )
        self._ensure_column("cohorts", "candidate_run_keys", "TEXT NOT NULL DEFAULT '[]'")
        self._ensure_column("cohorts", "protocol_hash", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("cohorts", "split_hash", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("cohorts", "verifier_binary_hash", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("cohorts", "certificate_hashes", "TEXT NOT NULL DEFAULT '[]'")
        self._ensure_column("consumption", "commitment", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("consumption", "request_id", "TEXT")
        self.db.execute(
            "UPDATE consumption SET commitment=(SELECT commitment FROM cohorts "
            "WHERE cohorts.cohort_id=consumption.cohort_id) WHERE commitment=''"
        )
        with contextlib.suppress(sqlite3.DatabaseError):
            self.db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS cohorts_commitment_unique ON cohorts(commitment)"
            )
        with contextlib.suppress(sqlite3.DatabaseError):
            self.db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS consumption_commitment_unique "
                "ON consumption(commitment)"
            )
        self.db.commit()

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _ensure_key(self) -> None:
        if self.key_path.exists():
            return
        key = Ed25519PrivateKey.generate()
        raw = key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        os.chmod(self.key_path, 0o600)

    def _key(self) -> Ed25519PrivateKey:
        return Ed25519PrivateKey.from_private_bytes(self.key_path.read_bytes())

    def public_key_hex(self) -> str:
        pub = self._key().public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return pub.hex()

    # ----------------------------------------------------------- local meta
    def _meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def _set_meta(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def _local_generation(self) -> int:
        return int(self._meta("anchor_generation") or 0)

    def _guard_ready(self) -> None:
        head = self.anchor.head()
        if self.requires_reconcile or self.root_mismatch:
            raise SealedError(
                "RECONCILE_REQUIRED",
                "local registry is behind or copied from another root; run reconcile_against_anchor()",
            )
        if head and int(head["generation"]) > self._local_generation():
            raise SealedError(
                "RECONCILE_REQUIRED",
                "local registry is behind the independent anchor; run reconcile_against_anchor()",
            )

    def _read_pending(self) -> dict | None:
        if not self.pending_path.exists():
            return None
        with contextlib.suppress(Exception):
            return json.loads(self.pending_path.read_text())
        return None

    def _write_pending(self, pending: dict) -> None:
        tmp = self.root / "pending_consumption.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, json.dumps(pending, sort_keys=True).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self.pending_path)
        os.chmod(self.pending_path, 0o600)
        _fsync_dir(self.root)

    def _clear_pending(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.pending_path.unlink()

    # --------------------------------------------------------------- cohort
    def _cohort_row(self, cohort_id: str):
        return self.db.execute(
            "SELECT cohort_id,program_id,cohort_index,commitment,alpha_num,alpha_den,state,"
            "candidate_run_keys,protocol_hash,split_hash,verifier_binary_hash,certificate_hashes "
            "FROM cohorts WHERE cohort_id=?",
            (cohort_id,),
        ).fetchone()

    # ------------------------------------------------------ lineage and events
    @staticmethod
    def _validate_lineage_fields(
        program_id: str,
        candidate_run_keys: list[str],
        protocol_hash: str,
        split_hash: str,
        verifier_binary_hash: str,
        certificate_hashes: list[str],
    ) -> None:
        """Fail closed on unsigned code or invalid lineage before any freeze."""
        problems: list[str] = []
        if not isinstance(program_id, str) or not program_id.strip():
            problems.append("program_id")
        if not isinstance(candidate_run_keys, (list, tuple)) or not candidate_run_keys:
            problems.append("candidate_run_keys")
        elif not all(_is_hex64(value) for value in candidate_run_keys):
            problems.append("candidate_run_keys")
        if not _is_hex64(protocol_hash):
            problems.append("protocol_hash")
        if not _is_hex64(split_hash):
            problems.append("split_hash")
        if not _is_hex64(verifier_binary_hash):
            problems.append("verifier_binary_hash")
        if not isinstance(certificate_hashes, (list, tuple)) or not certificate_hashes:
            problems.append("certificate_hashes")
        elif not all(_is_hex64(value) for value in certificate_hashes):
            problems.append("certificate_hashes")
        if problems:
            raise SealedError(
                "INVALID_LINEAGE",
                f"missing or unsigned lineage fields {sorted(set(problems))}",
            )

    def _lineage_evidence(self, cohort) -> dict:
        (
            cohort_id,
            program_id,
            cohort_index,
            commitment,
            alpha_num,
            alpha_den,
            _state,
            candidate_keys_text,
            protocol_hash,
            split_hash,
            verifier_binary_hash,
            certificate_text,
        ) = cohort
        candidate_keys = json.loads(candidate_keys_text)
        certificate_hashes = json.loads(certificate_text)
        self._validate_lineage_fields(
            program_id,
            candidate_keys,
            protocol_hash,
            split_hash,
            verifier_binary_hash,
            certificate_hashes,
        )
        return {
            "cohort_id": cohort_id,
            "program_id": program_id,
            "cohort_index": int(cohort_index),
            "window_commitment": commitment,
            "alpha_num": int(alpha_num),
            "alpha_den": int(alpha_den),
            "candidate_run_keys": list(candidate_keys),
            "candidate_run_keys_hash": hash_jcs(list(candidate_keys)),
            "protocol_hash": protocol_hash,
            "split_hash": split_hash,
            "verifier_binary_hash": verifier_binary_hash,
            "certificate_hashes_hash": hash_jcs(list(certificate_hashes)),
        }

    def _append_event(self, event_type: str, cohort_id: str, payload: dict) -> dict:
        """Append an audit event to the independent anchor.

        Events are separate from scientific metrics. They never mutate a stored
        decision or batch.
        """
        self._guard_ready()
        body = {"type": event_type, "cohort_id": cohort_id, **payload}
        record = self.anchor.append(body)
        acknowledged = self.anchor.acknowledge(record)
        if acknowledged is None:
            raise SealedError(
                "ANCHOR_UNAVAILABLE",
                "event acknowledgement missing; no event released",
            )
        self._set_meta("anchor_generation", str(acknowledged["generation"]))
        self.db.commit()
        self.local_generation = int(acknowledged["generation"])
        return {
            "event_type": event_type,
            "cohort_id": cohort_id,
            "anchor_hash": acknowledged["anchor_hash"],
            "anchor_generation": acknowledged["generation"],
            "payload": body,
        }

    def _frozen_before_access(self, commitment: str) -> bool:
        """True only when the anchor shows freeze strictly before consumption."""
        records = self.anchor.records()
        frozen_generation = None
        consumed_generation = None
        for record in records:
            payload = record.get("payload", {})
            if payload.get("window_commitment") != commitment:
                continue
            if payload.get("type") == "CohortFrozen" and frozen_generation is None:
                frozen_generation = int(record["generation"])
            elif payload.get("type") == "OOSConsumed":
                consumed_generation = int(record["generation"])
        return bool(
            frozen_generation is not None
            and consumed_generation is not None
            and frozen_generation < consumed_generation
        )

    def record_override(
        self,
        cohort_id: str,
        override: dict,
        *,
        authorized_by: str,
        reason: str,
    ) -> dict:
        """Record an authorized operator override as a separate event.

        The original signed decision/batch is untouched. An override may never
        carry a scientific metric: that would be a silent metric change.
        """
        if not isinstance(override, dict) or not override:
            raise SealedError("INVALID_DATA", "override must be a non-empty object")
        _reject_forbidden_keys(override, "override")
        if not isinstance(authorized_by, str) or not authorized_by.strip():
            raise SealedError("WAITING_AUTHORIZED_COHORT", "authorized_by is required")
        if not isinstance(reason, str) or not reason.strip():
            raise SealedError("INVALID_DATA", "override reason is required")
        cohort = self._cohort_row(cohort_id)
        if cohort is None:
            raise SealedError("WAITING_FRESH_OOS", "no frozen cohort")
        decision = self.db.execute(
            "SELECT decision_json FROM decisions WHERE cohort_id=?", (cohort_id,)
        ).fetchone()
        batch = self.db.execute(
            "SELECT batch_json FROM batches WHERE cohort_id=?", (cohort_id,)
        ).fetchone()
        override_id = hash_jcs(
            {
                "cohort_id": cohort_id,
                "override": override,
                "authorized_by": authorized_by,
                "reason": reason,
                "created_ns": _now(),
            }
        )
        event = self._append_event(
            "OperatorOverride",
            cohort_id,
            {
                "override_id": override_id,
                "override": override,
                "authorized_by": authorized_by,
                "reason": reason,
                "window_commitment": cohort[3],
                "decision_hash": hash_jcs(json.loads(decision[0])) if decision else None,
                "batch_hash": hash_jcs(json.loads(batch[0])) if batch else None,
            },
        )
        self.db.execute(
            "INSERT INTO overrides VALUES(?,?,?,?,?,?,?)",
            (
                override_id,
                cohort_id,
                json.dumps(override, sort_keys=True),
                authorized_by,
                reason,
                event["anchor_hash"],
                _now(),
            ),
        )
        self.db.commit()
        return {
            "override_id": override_id,
            "event_type": "OperatorOverride",
            "cohort_id": cohort_id,
            "anchor_hash": event["anchor_hash"],
            "anchor_generation": event["anchor_generation"],
            "override": override,
            "authorized_by": authorized_by,
            "reason": reason,
        }

    def freeze_cohort(
        self,
        cohort_id: str,
        program_id: str,
        cohort_index: int,
        candidate_run_keys: list[str],
        protocol_hash: str,
        split_hash: str,
        verifier_binary_hash: str,
        certificate_hashes: list[str],
    ) -> dict:
        self._guard_ready()
        self._validate_lineage_fields(
            program_id,
            list(candidate_run_keys),
            protocol_hash,
            split_hash,
            verifier_binary_hash,
            list(certificate_hashes),
        )
        candidate_keys_text = json.dumps(list(candidate_run_keys), sort_keys=True)
        certificate_text = json.dumps(list(certificate_hashes), sort_keys=True)
        alpha_den = 2 ** (cohort_index + 1)
        commitment = hash_jcs(
            {
                "program_id": program_id,
                "cohort_index": cohort_index,
                "candidate_run_keys": candidate_run_keys,
                "protocol_hash": protocol_hash,
                "split_hash": split_hash,
                "verifier_binary_hash": verifier_binary_hash,
                "certificate_hashes": certificate_hashes,
                "alpha_num": 5,
                "alpha_den": 100 * alpha_den,
            }
        )
        if self.db.execute("SELECT 1 FROM cohorts WHERE cohort_id=?", (cohort_id,)).fetchone():
            raise SealedError("PROTOCOL_VIOLATION", "cohort already exists")
        if self.db.execute(
            "SELECT 1 FROM cohorts WHERE program_id=? AND cohort_index=?",
            (program_id, cohort_index),
        ).fetchone():
            raise SealedError("PROTOCOL_VIOLATION", "window already consumed or frozen")
        if self.db.execute(
            "SELECT 1 FROM cohorts WHERE commitment=?", (commitment,)
        ).fetchone():
            raise SealedError("PROTOCOL_VIOLATION", "window commitment already frozen")
        if self.anchor.find_by_commitment(commitment):
            raise SealedError("PROTOCOL_VIOLATION", "window commitment already anchored")
        if self.anchor.find_window(program_id, cohort_index):
            raise SealedError("PROTOCOL_VIOLATION", "window already anchored")
        try:
            self.db.execute(
                "INSERT INTO cohorts(cohort_id,program_id,cohort_index,commitment,alpha_num,"
                "alpha_den,state,created_ns,candidate_run_keys,protocol_hash,split_hash,"
                "verifier_binary_hash,certificate_hashes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    cohort_id,
                    program_id,
                    cohort_index,
                    commitment,
                    5,
                    100 * alpha_den,
                    "FROZEN",
                    _now(),
                    candidate_keys_text,
                    protocol_hash,
                    split_hash,
                    verifier_binary_hash,
                    certificate_text,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise SealedError("PROTOCOL_VIOLATION", "window already frozen") from exc
        self.db.commit()
        record = self.anchor.append(
            {
                "type": "CohortFrozen",
                "cohort_id": cohort_id,
                "program_id": program_id,
                "cohort_index": cohort_index,
                "window_commitment": commitment,
                "candidate_run_keys": candidate_run_keys,
                "protocol_hash": protocol_hash,
                "split_hash": split_hash,
                "verifier_binary_hash": verifier_binary_hash,
                "certificate_hashes": certificate_hashes,
                "alpha_num": 5,
                "alpha_den": 100 * alpha_den,
            }
        )
        self.anchor.acknowledge(record)
        self._set_meta("anchor_generation", str(record["generation"]))
        self.db.commit()
        receipt = self.prepare_read_receipt(cohort_id)
        return {
            "cohort_id": cohort_id,
            "state": "FROZEN",
            "commitment": commitment,
            "alpha_num": 5,
            "alpha_den": 100 * alpha_den,
            "anchor_hash": record["anchor_hash"],
            "anchor_generation": record["generation"],
            "receipt_hash": receipt["receipt_hash"],
        }

    # ---------------------------------------------------- pre-access receipt
    def _build_receipt_identity(self, cohort) -> dict:
        (
            cohort_id,
            program_id,
            cohort_index,
            commitment,
            alpha_num,
            alpha_den,
            _state,
            candidate_keys_text,
            protocol_hash,
            split_hash,
            verifier_binary_hash,
            certificate_text,
        ) = cohort
        return {
            "cohort_id": cohort_id,
            "program_id": program_id,
            "cohort_index": cohort_index,
            "window_commitment": commitment,
            "candidate_run_keys_hash": hash_jcs(json.loads(candidate_keys_text)),
            "protocol_hash": protocol_hash,
            "data_hash": split_hash,
            "verifier_binary_hash": verifier_binary_hash,
            "certificate_hashes_hash": hash_jcs(json.loads(certificate_text)),
            "alpha_num": alpha_num,
            "alpha_den": alpha_den,
        }

    def _ensure_read_receipt(self, cohort) -> dict:
        cohort_id = cohort[0]
        identity = self._build_receipt_identity(cohort)
        receipt_hash = hash_jcs(identity)
        row = self.db.execute(
            "SELECT receipt_hash,receipt_json,signature,created_ns FROM read_receipts "
            "WHERE cohort_id=?",
            (cohort_id,),
        ).fetchone()
        if row:
            if row[0] != receipt_hash:
                raise SealedError("PROTOCOL_VIOLATION", "pre-access receipt is immutable")
            body = json.loads(row[1])
            return {
                **identity,
                "receipt_hash": row[0],
                "signature": row[2],
                "public_key": self.public_key_hex(),
                "prepared_ns": row[3],
                **body,
            }
        signature = self._key().sign(canonical_bytes(identity)).hex()
        created = _now()
        self.db.execute(
            "INSERT INTO read_receipts VALUES(?,?,?,?,?,?)",
            (
                cohort_id,
                identity["window_commitment"],
                receipt_hash,
                json.dumps(identity, sort_keys=True),
                signature,
                created,
            ),
        )
        return {
            **identity,
            "receipt_hash": receipt_hash,
            "signature": signature,
            "public_key": self.public_key_hex(),
            "prepared_ns": created,
        }

    def prepare_read_receipt(self, cohort_id: str) -> dict:
        """Commit and return the immutable window identity plus exact alpha spend.

        No experimental read is released by this call. ``spend_consumption``
        must complete and acknowledge the anchor before ``evaluation_allowed``
        turns true.
        """
        self._guard_ready()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            cohort = self._cohort_row(cohort_id)
            if cohort is None:
                raise SealedError("WAITING_FRESH_OOS", "no frozen cohort")
            receipt = self._ensure_read_receipt(cohort)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return receipt

    # ------------------------------------------------------------ consumption
    def _consumption_row(self, cohort_id: str):
        return self.db.execute(
            "SELECT cohort_id,receipt_hash,alpha_spent_num,alpha_spent_den,anchor_hash,"
            "commitment,request_id FROM consumption WHERE cohort_id=?",
            (cohort_id,),
        ).fetchone()

    def _consumption_response(self, row, *, reused: bool, recovered: bool = False) -> dict:
        return {
            "cohort_id": row[0],
            "receipt_hash": row[1],
            "alpha_spent_num": row[2],
            "alpha_spent_den": row[3],
            "anchor_hash": row[4],
            "commitment": row[5],
            "request_id": row[6],
            "reused": reused,
            "recovered": recovered,
        }

    def spend_consumption(
        self, cohort_id: str, acknowledged: bool, *, request_id: str | None = None
    ) -> dict:
        if not acknowledged:
            raise SealedError("WAITING_AUTHORIZED_COHORT", "anchor receipt not acknowledged")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            cohort = self._cohort_row(cohort_id)
            if cohort is None:
                raise SealedError("WAITING_FRESH_OOS", "no frozen cohort")
            commitment = cohort[3]
            alpha_num, alpha_den = cohort[4], cohort[5]
            existing = self._consumption_row(cohort_id)
            if existing:
                self._guard_ready()
                if request_id is not None and existing[6] not in (None, request_id):
                    self.db.rollback()
                    raise SealedError("ALREADY_CONSUMED", "window already consumed")
                self.db.rollback()
                pending = self._read_pending()
                if pending and pending.get("cohort_id") == cohort_id:
                    self._clear_pending()
                return self._consumption_response(existing, reused=True)
            other = self.db.execute(
                "SELECT cohort_id FROM consumption WHERE commitment=?", (commitment,)
            ).fetchone()
            if other is not None:
                raise SealedError(
                    "RECONCILE_REQUIRED",
                    "window commitment consumed under a different cohort identity",
                )
            receipt = self._ensure_read_receipt(cohort)
            receipt_hash = receipt["receipt_hash"]
            pending = self._read_pending()
            anchor_consumed = self.anchor.latest_consumption(commitment)
            if anchor_consumed is not None:
                payload = anchor_consumed.get("payload", {})
                pending_matches = bool(
                    pending
                    and pending.get("receipt_hash") == receipt_hash
                    and pending.get("cohort_id") == cohort_id
                    and pending.get("commitment") == commitment
                    and not self.root_mismatch
                )
                if payload.get("receipt_hash") == receipt_hash and pending_matches:
                    effective_request = pending.get("request_id")
                    if request_id is not None and effective_request not in (None, request_id):
                        raise SealedError("ALREADY_CONSUMED", "window already consumed")
                    self.db.execute(
                        "INSERT INTO consumption VALUES(?,?,?,?,?,?,?,?)",
                        (
                            cohort_id,
                            receipt_hash,
                            alpha_num,
                            alpha_den,
                            anchor_consumed["anchor_hash"],
                            _now(),
                            commitment,
                            effective_request,
                        ),
                    )
                    self.db.execute(
                        "UPDATE cohorts SET state='CONSUMED_RUNNING' WHERE cohort_id=?",
                        (cohort_id,),
                    )
                    self._set_meta("anchor_generation", str(anchor_consumed["generation"]))
                    self.db.commit()
                    self._clear_pending()
                    self.local_generation = int(anchor_consumed["generation"])
                    head = self.anchor.head()
                    self.requires_reconcile = bool(
                        (head and int(head["generation"]) > self.local_generation)
                        or self.root_mismatch
                    )
                    return {
                        "cohort_id": cohort_id,
                        "receipt_hash": receipt_hash,
                        "alpha_spent_num": alpha_num,
                        "alpha_spent_den": alpha_den,
                        "anchor_hash": anchor_consumed["anchor_hash"],
                        "commitment": commitment,
                        "request_id": effective_request,
                        "reused": False,
                        "recovered": True,
                        "anchor_generation": anchor_consumed["generation"],
                    }
                raise SealedError(
                    "RECONCILE_REQUIRED",
                    "independent anchor already consumed this window; run reconcile_against_anchor()",
                )
            self._guard_ready()
            if pending:
                if (
                    pending.get("receipt_hash") != receipt_hash
                    or pending.get("commitment") != commitment
                    or (
                        request_id is not None
                        and pending.get("request_id") not in (None, request_id)
                    )
                ):
                    raise SealedError("ALREADY_CONSUMED", "consumption already in progress")
            else:
                pending = {
                    "cohort_id": cohort_id,
                    "commitment": commitment,
                    "receipt_hash": receipt_hash,
                    "request_id": request_id,
                    "created_ns": _now(),
                }
                self._write_pending(pending)
            record = self.anchor.append(
                {
                    "type": "OOSConsumed",
                    "cohort_id": cohort_id,
                    "window_commitment": commitment,
                    "receipt_hash": receipt_hash,
                    "alpha_num": alpha_num,
                    "alpha_den": alpha_den,
                    "request_id": request_id,
                }
            )
            ack = self.anchor.acknowledge(record)
            if ack is None:
                raise SealedError(
                    "ANCHOR_UNAVAILABLE",
                    "independent anchor acknowledgement missing; no read released",
                )
            self.db.execute(
                "INSERT INTO consumption VALUES(?,?,?,?,?,?,?,?)",
                (
                    cohort_id,
                    receipt_hash,
                    alpha_num,
                    alpha_den,
                    ack["anchor_hash"],
                    _now(),
                    commitment,
                    request_id,
                ),
            )
            self.db.execute(
                "UPDATE cohorts SET state='CONSUMED_RUNNING' WHERE cohort_id=?", (cohort_id,)
            )
            self._set_meta("anchor_generation", str(ack["generation"]))
            self.db.commit()
            self._clear_pending()
            self.local_generation = int(ack["generation"])
        except Exception:
            self.db.rollback()
            raise
        return {
            "cohort_id": cohort_id,
            "receipt_hash": receipt_hash,
            "alpha_spent_num": alpha_num,
            "alpha_spent_den": alpha_den,
            "anchor_hash": ack["anchor_hash"],
            "commitment": commitment,
            "request_id": request_id,
            "reused": False,
            "recovered": False,
            "anchor_generation": ack["generation"],
        }

    def evaluation_allowed(self, cohort_id: str) -> dict:
        """Return whether an experimental read may be released.

        True only when a committed pre-access receipt, a local consumption row,
        and matching independent anchor acknowledgement all exist.
        """
        try:
            self._guard_ready()
        except SealedError as exc:
            return {"allowed": False, "reason": exc.code, "cohort_id": cohort_id}
        cohort = self._cohort_row(cohort_id)
        if cohort is None:
            return {"allowed": False, "reason": "WAITING_FRESH_OOS", "cohort_id": cohort_id}
        receipt = self.db.execute(
            "SELECT receipt_hash FROM read_receipts WHERE cohort_id=?", (cohort_id,)
        ).fetchone()
        if receipt is None:
            return {"allowed": False, "reason": "NO_PRE_ACCESS_RECEIPT", "cohort_id": cohort_id}
        consumed = self._consumption_row(cohort_id)
        if consumed is None:
            return {
                "allowed": False,
                "reason": "WAITING_AUTHORIZED_COHORT",
                "cohort_id": cohort_id,
                "receipt_hash": receipt[0],
            }
        anchor_record = self.anchor.latest_consumption(cohort[3])
        if (
            anchor_record is None
            or anchor_record.get("payload", {}).get("receipt_hash") != consumed[1]
        ):
            return {
                "allowed": False,
                "reason": "ANCHOR_UNAVAILABLE",
                "cohort_id": cohort_id,
                "receipt_hash": receipt[0],
            }
        return {
            "allowed": True,
            "reason": None,
            "cohort_id": cohort_id,
            "receipt_hash": consumed[1],
            "consumption_receipt": consumed[1],
            "anchor_hash": consumed[4],
            "anchor_generation": anchor_record["generation"],
            "window_commitment": cohort[3],
        }

    # -------------------------------------------------------------- decision
    def decide(
        self,
        cohort_id: str,
        candidate_daily: list[float],
        control_daily: list[float],
        capital: float,
        economic_minimum: float,
        b: int = 512,
        seed: int = 0,
        power: float = 0.8,
        *,
        gate_evidence: dict | None = None,
        candidate_run_keys: list[str] | None = None,
        candidate_id: str = "CANDIDATE",
    ) -> dict:
        self._guard_ready()
        if self.db.execute("SELECT 1 FROM decisions WHERE cohort_id=?", (cohort_id,)).fetchone():
            raise SealedError("PROTOCOL_VIOLATION", "decision already issued")
        consumed = self._consumption_row(cohort_id)
        if consumed is None:
            raise SealedError("WAITING_AUTHORIZED_COHORT", "no pre-access consumption receipt")
        anchor_record = self.anchor.latest_consumption(consumed[5])
        if (
            anchor_record is None
            or anchor_record.get("payload", {}).get("receipt_hash") != consumed[1]
        ):
            raise SealedError("ANCHOR_UNAVAILABLE", "independent anchor acknowledgement missing")
        cohort = self._cohort_row(cohort_id)
        if cohort is None:
            raise SealedError("WAITING_FRESH_OOS", "no frozen cohort")
        lineage = self._lineage_evidence(cohort)
        candidate_series = _finite_series(candidate_daily, "candidate_daily")
        control_series = _finite_series(control_daily, "control_daily")
        if len(candidate_series) < 3 or len(control_series) < 3:
            raise SealedError("INSUFFICIENT_HISTORY")
        lineage_bound = False
        if candidate_run_keys is not None:
            expected_key = _candidate_run_key(
                {"candidate_id": str(candidate_id), "daily": candidate_series}
            )
            if list(candidate_run_keys) != [expected_key]:
                raise SealedError("CHANGED_INPUTS", "candidate series do not match the frozen inputs")
            if expected_key not in lineage["candidate_run_keys"]:
                raise SealedError("CHANGED_INPUTS", "candidate run key is not in the frozen cohort")
            lineage_bound = True
        from .gates import evaluate_gates, validate_gate_evidence

        validate_gate_evidence(gate_evidence, lineage["verifier_binary_hash"])
        returns_c = [x / capital for x in candidate_series]
        returns_k = [x / capital for x in control_series]
        p_c = studentized_bootstrap_p(returns_c, b=b, seed=seed).p_upper
        p_k = studentized_bootstrap_p(returns_k, b=b, seed=seed + 1).p_upper
        p = iut(p_c, p_k)
        mean = sum(returns_c) / len(returns_c)
        se = studentized_bootstrap_p(returns_c, b=b, seed=seed).se
        ci_low = mean - 1.96 * se
        ci_high = mean + 1.96 * se
        verdict = classify(p, power, ci_low, ci_high, economic_minimum)
        holm_rejected = holm([p], cohort[4] / cohort[5])[0]
        derived = {
            "G1": {"lineage.valid": lineage_bound},
            "G4": {
                "cohort.frozen_before_access": self._frozen_before_access(cohort[3]),
                "window.valid_uncontaminated_spent_receipt": bool(
                    self.evaluation_allowed(cohort_id)["allowed"]
                ),
                "holm.conservative_p_rejected_null": bool(holm_rejected),
                "economic_effect.both_minima_pass": bool(ci_low > economic_minimum),
            },
        }
        gate_result = evaluate_gates(gate_evidence, derived)
        promotion_allowed = bool(gate_result["all_pass"] and lineage_bound)
        scientific_verdict = verdict if promotion_allowed else (
            "INSUFFICIENT_EVIDENCE" if verdict == "EDGE" else verdict
        )
        scope = {
            "cohort_id": cohort_id,
            "program_id": lineage["program_id"],
            "cohort_index": lineage["cohort_index"],
            "window_commitment": cohort[3],
            "alpha_num": cohort[4],
            "alpha_den": cohort[5],
            "capital": float(capital),
            "economic_minimum": float(economic_minimum),
            "b": int(b),
            "seed": int(seed),
            "power": float(power),
        }
        evidence_hashes = {
            **lineage,
            "control_hash": hash_jcs([float(value) for value in control_series]),
            "candidate_run_keys_hash": (
                hash_jcs(list(candidate_run_keys)) if candidate_run_keys is not None else None
            ),
            "gate_evidence_hash": gate_result.get("evidence_hash"),
        }
        body = {
            "cohort_id": cohort_id,
            "verdict": verdict,
            "scientific_verdict": scientific_verdict,
            "p_profit": p_c,
            "p_control": p_k,
            "p_iut": p,
            "ci_low": ci_low,
            "ci_high": ci_high,
            "mean_daily_return": mean,
            "b": b,
            "seed": seed,
            "power": float(power),
            "scope": scope,
            "scope_hash": hash_jcs(scope),
            "evidence_hashes": evidence_hashes,
            "evidence_hash": hash_jcs(evidence_hashes),
            "gates": gate_result,
            "promotion_allowed": promotion_allowed,
            "consumption_receipt": consumed[1],
            "task14": {
                "method": "TASK14_PAIRED_IUT_HOLM_V1",
                "holm_rejected": bool(holm_rejected),
                "alpha_num": cohort[4],
                "alpha_den": cohort[5],
            },
            "created_ns": _now(),
        }
        signature = self._key().sign(canonical_bytes(body)).hex()
        inserted = self.db.execute(
            "INSERT OR IGNORE INTO decisions VALUES(?,?,?,?,?)",
            (cohort_id, verdict, json.dumps(body, sort_keys=True), signature, _now()),
        )
        if inserted.rowcount != 1:
            self.db.rollback()
            raise SealedError("PROTOCOL_VIOLATION", "decision already issued")
        self.db.execute("UPDATE cohorts SET state='RELEASED' WHERE cohort_id=?", (cohort_id,))
        self.db.commit()
        return {**body, "signature": signature, "public_key": self.public_key_hex()}

    def verify(self, decision: dict) -> bool:
        try:
            public_key = decision["public_key"]
            if public_key != self.public_key_hex() and public_key not in self.trusted_keys():
                return False
            body = {k: v for k, v in decision.items() if k not in ("signature", "public_key")}
            pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key))
            pub.verify(bytes.fromhex(decision["signature"]), canonical_bytes(body))
            return True
        except Exception:
            return False

    # ------------------------------------------------------------- recovery
    def restore_from_old(self, local_generation: int, anchor_generation: int) -> dict:
        if local_generation < anchor_generation:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute(
                    "UPDATE cohorts SET state='WAITING_FRESH_OOS' WHERE state='CONSUMED_RUNNING'"
                )
                self._set_meta("restore_blocked", "1")
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
            self.requires_reconcile = True
            return {
                "state": "WAITING_FRESH_OOS",
                "alpha_counter_decreased": False,
                "reconcile_required": True,
            }
        return {
            "state": "UNCHANGED",
            "alpha_counter_decreased": False,
            "reconcile_required": self.requires_reconcile,
        }

    def reconcile_against_anchor(self) -> dict:
        """Replay the independent anchor into the local registry.

        The consumed window stays consumed and alpha is never reset. Any local
        row that contradicts the anchor aborts the reconciliation.
        """
        chain = self.anchor.verify_chain()
        if not chain["valid"]:
            raise SealedError("ANCHOR_CORRUPT", "; ".join(chain["errors"]))
        restored = 0
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for record in self.anchor.records():
                payload = record.get("payload", {})
                kind = payload.get("type")
                cohort_id = payload.get("cohort_id")
                commitment = payload.get("window_commitment") or record.get("window_commitment")
                if kind == "CohortFrozen":
                    local = self._cohort_row(cohort_id)
                    if local is None:
                        self.db.execute(
                            "INSERT INTO cohorts(cohort_id,program_id,cohort_index,commitment,"
                            "alpha_num,alpha_den,state,created_ns,candidate_run_keys,"
                            "protocol_hash,split_hash,verifier_binary_hash,certificate_hashes) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                cohort_id,
                                payload.get("program_id", ""),
                                int(payload.get("cohort_index", 0)),
                                commitment,
                                int(payload.get("alpha_num", 5)),
                                int(payload.get("alpha_den", 0)),
                                "FROZEN",
                                _now(),
                                json.dumps(payload.get("candidate_run_keys", []), sort_keys=True),
                                payload.get("protocol_hash", ""),
                                payload.get("split_hash", ""),
                                payload.get("verifier_binary_hash", ""),
                                json.dumps(payload.get("certificate_hashes", []), sort_keys=True),
                            ),
                        )
                    elif local[3] != commitment:
                        raise SealedError(
                            "PROTOCOL_VIOLATION",
                            f"local cohort {cohort_id} contradicts the anchor commitment",
                        )
                elif kind == "OOSConsumed":
                    existing = self._consumption_row(cohort_id)
                    if existing is None:
                        by_commitment = self.db.execute(
                            "SELECT cohort_id FROM consumption WHERE commitment=?", (commitment,)
                        ).fetchone()
                        if by_commitment is None:
                            self.db.execute(
                                "INSERT INTO consumption VALUES(?,?,?,?,?,?,?,?)",
                                (
                                    cohort_id,
                                    payload.get("receipt_hash"),
                                    int(payload.get("alpha_num", 0)),
                                    int(payload.get("alpha_den", 1)),
                                    record["anchor_hash"],
                                    _now(),
                                    commitment,
                                    payload.get("request_id"),
                                ),
                            )
                            restored += 1
                        elif by_commitment[0] != cohort_id:
                            raise SealedError(
                                "PROTOCOL_VIOLATION",
                                "anchor commitment is bound to a different cohort identity",
                            )
                    elif existing[1] != payload.get("receipt_hash"):
                        raise SealedError(
                            "PROTOCOL_VIOLATION",
                            "local consumption receipt contradicts the anchor",
                        )
            rows = self.db.execute("SELECT cohort_id FROM consumption").fetchall()
            for (cohort_id,) in rows:
                decision = self.db.execute(
                    "SELECT 1 FROM decisions WHERE cohort_id=?", (cohort_id,)
                ).fetchone()
                batch = self.db.execute(
                    "SELECT 1 FROM batches WHERE cohort_id=?", (cohort_id,)
                ).fetchone()
                state = "RELEASED" if (decision or batch) else "CONSUMED_RUNNING"
                self.db.execute(
                    "UPDATE cohorts SET state=? WHERE cohort_id=?", (state, cohort_id)
                )
            head = self.anchor.head()
            generation = int(head["generation"]) if head else 0
            self._set_meta("anchor_generation", str(generation))
            self._set_meta("registry_root", str(self.root.resolve()))
            self._set_meta("reconciled_ns", _now())
            self.db.execute("DELETE FROM meta WHERE key='restore_blocked'")
            self._clear_pending()
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        self.requires_reconcile = False
        self.root_mismatch = False
        self.local_generation = generation
        total = sum(
            (
                Fraction(row[0], row[1])
                for row in self.db.execute(
                    "SELECT alpha_spent_num,alpha_spent_den FROM consumption"
                ).fetchall()
            ),
            Fraction(0, 1),
        )
        return {
            "status": "RECONCILED",
            "anchor_generation": generation,
            "restored_consumptions": restored,
            "consumption_count": len(
                self.db.execute("SELECT 1 FROM consumption").fetchall()
            ),
            "alpha_spent": str(total),
            "alpha_counter_decreased": False,
        }

    # ----------------------------------------------------------------- trials
    def reserve_trial(self, trial_key: str, cohort_id: str) -> bool:
        try:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("INSERT INTO trials VALUES(?,?,?)", (trial_key, cohort_id, _now()))
            self.db.commit()
            return True
        except sqlite3.IntegrityError:
            self.db.rollback()
            return False
        except Exception:
            self.db.rollback()
            raise

    def lease(self, lease_id: str, generation: int) -> None:
        self.db.execute("INSERT OR REPLACE INTO leases VALUES(?,?,?)", (lease_id, generation, _now()))
        self.db.commit()

    def stale_result(self, lease_id: str, generation: int) -> bool:
        row = self.db.execute("SELECT generation FROM leases WHERE lease_id=?", (lease_id,)).fetchone()
        return row is None or row[0] != generation

    # ------------------------------------------------------- trusted signers
    def trust_key(self, public_key_hex: str, label: str = "evaluator") -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO trusted_keys VALUES(?,?,?)",
            (public_key_hex, label, _now()),
        )
        self.db.commit()

    def trusted_keys(self) -> set[str]:
        return {
            row[0] for row in self.db.execute("SELECT public_key FROM trusted_keys").fetchall()
        }

    # ----------------------------------------------------------- signed batch
    @staticmethod
    def _candidate_hash(daily: list) -> str:
        return hash_jcs([float(value) for value in daily])

    @staticmethod
    def _batch_input_hash(
        cohort_id: str,
        commitment: str,
        receipt_hash: str,
        control_hash: str,
        candidates: list[dict],
        capital: float,
        economic_minimum: float,
        b: int,
        seed: int,
        power: float,
        *,
        gate_evidence_hash: str | None = None,
        data_manifest_hash: str | None = None,
        candidate_run_keys_hash: str | None = None,
    ) -> str:
        body = {
            "cohort_id": cohort_id,
            "window_commitment": commitment,
            "consumption_receipt": receipt_hash,
            "control_hash": control_hash,
            "candidates": [
                {
                    "candidate_id": item["candidate_id"],
                    "candidate_hash": item["candidate_hash"],
                }
                for item in candidates
            ],
            "capital": float(capital),
            "economic_minimum": float(economic_minimum),
            "b": int(b),
            "seed": int(seed),
            "power": float(power),
        }
        if gate_evidence_hash is not None:
            body["gate_evidence_hash"] = gate_evidence_hash
        if data_manifest_hash is not None:
            body["data_manifest_hash"] = data_manifest_hash
        if candidate_run_keys_hash is not None:
            body["candidate_run_keys_hash"] = candidate_run_keys_hash
        return hash_jcs(body)

    @staticmethod
    def _validate_data_manifest(
        manifest: dict | None, lineage: dict, candidate_ids: list[str]
    ) -> dict:
        """Validate OOS/development separation and return its evidence hash.

        A contaminated or unverifiable manifest is refused; it is never
        silently reduced to a warning.
        """
        if manifest is None:
            return {
                "verified": False,
                "contaminated": False,
                "reason": "DATA_MANIFEST_NOT_PROVIDED",
                "manifest_hash": None,
            }
        if not isinstance(manifest, dict):
            raise SealedError("INVALID_DATA", "data manifest must be an object")
        required = (
            "cohort_id",
            "window_commitment",
            "split_hash",
            "synthetic",
            "source",
            "oos_window",
            "development_windows",
            "candidate_sources",
            "control_source",
        )
        missing = [key for key in required if key not in manifest]
        if missing:
            raise SealedError("MISSING_DATA_MANIFEST_FIELDS", f"missing {sorted(missing)}")
        if manifest["cohort_id"] != lineage["cohort_id"]:
            raise SealedError("INVALID_LINEAGE", "data manifest cohort mismatch")
        if manifest["window_commitment"] != lineage["window_commitment"]:
            raise SealedError("INVALID_LINEAGE", "data manifest commitment mismatch")
        if manifest["split_hash"] != lineage["split_hash"]:
            raise SealedError("INVALID_LINEAGE", "data manifest split mismatch")
        if not isinstance(manifest["synthetic"], bool):
            raise SealedError("INVALID_DATA", "data manifest synthetic flag must be boolean")
        if not isinstance(manifest["source"], str) or not manifest["source"].strip():
            raise SealedError("INVALID_DATA", "data manifest source is empty")

        def window(value, name: str) -> tuple[str, str]:
            if (
                not isinstance(value, (list, tuple))
                or len(value) != 2
                or not all(isinstance(item, str) and item for item in value)
            ):
                raise SealedError("INVALID_DATA", f"{name} must be a [start,end] pair")
            if value[0] > value[1]:
                raise SealedError("INVALID_DATA", f"{name} start is after end")
            return (value[0], value[1])

        oos = window(manifest["oos_window"], "oos_window")
        development = manifest["development_windows"]
        if not isinstance(development, (list, tuple)):
            raise SealedError("INVALID_DATA", "development_windows must be a list")
        development_windows = [
            window(item, f"development_windows[{index}]")
            for index, item in enumerate(development)
        ]
        contaminating_windows = [
            item
            for item in development_windows
            if not (item[1] < oos[0] or item[0] > oos[1])
        ]
        candidate_sources = manifest["candidate_sources"]
        if not isinstance(candidate_sources, dict):
            raise SealedError("INVALID_DATA", "candidate_sources must be an object")
        if set(candidate_sources) != set(candidate_ids):
            raise SealedError(
                "CHANGED_INPUTS", "candidate_sources do not cover the released family"
            )
        contaminating_sources = sorted(
            candidate_id
            for candidate_id, source in candidate_sources.items()
            if source in _CONTAMINATING_SOURCES
        )
        control_source = manifest["control_source"]
        if control_source in _CONTAMINATING_SOURCES:
            contaminating_sources.append("control")
        unknown_sources = sorted(
            candidate_id
            for candidate_id, source in candidate_sources.items()
            if source not in _OOS_SOURCES and source not in _CONTAMINATING_SOURCES
        )
        if control_source not in _OOS_SOURCES and control_source not in _CONTAMINATING_SOURCES:
            unknown_sources.append("control")
        if unknown_sources:
            raise SealedError("INVALID_DATA", f"unknown data sources {unknown_sources}")
        contaminated = bool(contaminating_windows or contaminating_sources)
        return {
            "verified": True,
            "contaminated": contaminated,
            "reason": (
                "CONTAMINATED_INPUT"
                if contaminated
                else "OOS_DEVELOPMENT_SEPARATION_VERIFIED"
            ),
            "manifest_hash": hash_jcs(manifest),
            "oos_window": list(oos),
            "development_windows": [list(item) for item in development_windows],
            "overlapping_windows": [list(item) for item in contaminating_windows],
            "contaminating_sources": contaminating_sources,
            "synthetic": bool(manifest["synthetic"]),
        }

    @staticmethod
    def _holm_adjusted(pvals: list[float]) -> list[float]:
        m = len(pvals)
        order = sorted(range(m), key=lambda index: pvals[index])
        adjusted = [1.0] * m
        running = 0.0
        for rank, index in enumerate(order):
            value = min(1.0, (m - rank) * pvals[index])
            running = max(running, value)
            adjusted[index] = running
        return adjusted

    def release_batch(
        self,
        cohort_id: str,
        candidates: list[dict],
        control_daily: list[float],
        capital: float,
        economic_minimum: float,
        b: int = 512,
        seed: int = 0,
        power: float = 0.8,
        *,
        gate_evidence: dict | None = None,
        candidate_run_keys: list[str] | None = None,
        data_manifest: dict | None = None,
        request_id: str | None = None,
    ) -> dict:
        """Compute, sign and release one redacted multi-candidate batch.

        Invalid candidates remain in the Holm family with p=1.0. Raw candidate
        or control returns are never written to the batch or manifest. All
        decision metrics are derived here; a caller supplied metric, code
        payload, invalid lineage, missing gate field or unregistered key is
        refused.
        """
        self._guard_ready()
        cohort = self._cohort_row(cohort_id)
        if cohort is None:
            raise SealedError("WAITING_FRESH_OOS", "no frozen cohort")
        lineage = self._lineage_evidence(cohort)
        if not isinstance(candidates, (list, tuple)) or not candidates:
            raise SealedError("INVALID_CANDIDATE_FAMILY", "candidate family is empty")
        normalized: list[dict] = []
        seen_ids: set[str] = set()
        for index, candidate in enumerate(candidates):
            if not isinstance(candidate, dict):
                raise SealedError("INVALID_DATA", "candidate must be an object")
            unknown = set(candidate) - CANDIDATE_ALLOWED_KEYS
            if unknown:
                _reject_forbidden_keys(candidate, f"candidate[{index}]")
                raise SealedError(
                    "CALLER_METRICS_REJECTED",
                    f"candidate[{index}] has non-authoritative fields {sorted(unknown)}",
                )
            candidate_id = str(candidate.get("candidate_id", f"C{index}"))
            if not candidate_id or candidate_id in seen_ids:
                raise SealedError("INVALID_CANDIDATE_FAMILY", "candidate ids must be unique")
            seen_ids.add(candidate_id)
            normalized.append({"candidate_id": candidate_id, "daily": candidate.get("daily")})
        candidate_ids = [item["candidate_id"] for item in normalized]
        for item in normalized:
            try:
                series = _finite_series(
                    item.get("daily"), f"candidate[{item['candidate_id']}].daily"
                )
                item["series"] = series
                item["valid"] = len(series) >= 3
            except SealedError:
                item["series"] = []
                item["valid"] = False
            item["candidate_hash"] = (
                self._candidate_hash(item["series"])
                if item["valid"]
                else hash_jcs({"candidate_id": item["candidate_id"], "invalid": True})
            )

        control_series = _finite_series(control_daily, "control_daily")
        if len(control_series) < 3:
            raise SealedError("INSUFFICIENT_HISTORY", "control history too short")
        control_hash = hash_jcs(control_series)

        candidate_keys_hash = None
        lineage_bound = False
        expected_keys = [_candidate_run_key(item) for item in normalized]
        if candidate_run_keys is not None:
            supplied_keys = [str(key) for key in candidate_run_keys]
            if supplied_keys != expected_keys:
                self._append_event(
                    "ReleaseRefused",
                    cohort_id,
                    {
                        "reason": "CHANGED_INPUTS",
                        "detail": "candidate series do not match the declared frozen keys",
                    },
                )
                raise SealedError(
                    "CHANGED_INPUTS", "candidate series do not match the declared frozen keys"
                )
            frozen_keys = list(lineage["candidate_run_keys"])
            if any(key not in frozen_keys for key in expected_keys):
                self._append_event(
                    "ReleaseRefused",
                    cohort_id,
                    {
                        "reason": "CHANGED_INPUTS",
                        "detail": "candidate keys are not part of the frozen cohort",
                    },
                )
                raise SealedError(
                    "CHANGED_INPUTS", "candidate keys are not part of the frozen cohort"
                )
            candidate_keys_hash = hash_jcs(supplied_keys)
            lineage_bound = True

        data_status = self._validate_data_manifest(data_manifest, lineage, candidate_ids)
        if data_status["contaminated"]:
            self._append_event(
                "ReleaseRefused",
                cohort_id,
                {
                    "reason": "CONTAMINATED_INPUT",
                    "detail": "OOS and development inputs overlap",
                    "overlapping_windows": data_status.get("overlapping_windows"),
                    "contaminating_sources": data_status.get("contaminating_sources"),
                },
            )
            raise SealedError(
                "CONTAMINATED_INPUT",
                "OOS and development data are mixed; the frozen window is not authorized",
            )

        from .gates import evaluate_gates, validate_gate_evidence

        gate_structure = validate_gate_evidence(gate_evidence, lineage["verifier_binary_hash"])
        gate_evidence_hash = gate_structure.get("evidence_hash")

        consumed = self._consumption_row(cohort_id)
        if consumed is None:
            raise SealedError("WAITING_AUTHORIZED_COHORT", "no pre-access consumption receipt")
        anchor_record = self.anchor.latest_consumption(cohort[3])
        if (
            anchor_record is None
            or anchor_record.get("payload", {}).get("receipt_hash") != consumed[1]
        ):
            raise SealedError("ANCHOR_UNAVAILABLE", "independent anchor acknowledgement missing")
        input_hash = self._batch_input_hash(
            cohort_id,
            cohort[3],
            consumed[1],
            control_hash,
            [
                {
                    "candidate_id": item["candidate_id"],
                    "candidate_hash": item["candidate_hash"],
                }
                for item in normalized
            ],
            capital,
            economic_minimum,
            b,
            seed,
            power,
            gate_evidence_hash=gate_evidence_hash,
            data_manifest_hash=data_status["manifest_hash"],
            candidate_run_keys_hash=candidate_keys_hash,
        )
        batch_id = hash_jcs(
            {
                "cohort_id": cohort_id,
                "input_hash": input_hash,
                "consumption_receipt": consumed[1],
            }
        )
        existing = self.db.execute(
            "SELECT batch_id,input_hash,batch_json,signature,public_key FROM batches "
            "WHERE cohort_id=?",
            (cohort_id,),
        ).fetchone()
        if existing is not None:
            if existing[1] == input_hash:
                self._append_event(
                    "BatchRetry",
                    cohort_id,
                    {"batch_id": existing[0], "input_hash": input_hash},
                )
                return {
                    **json.loads(existing[2]),
                    "signature": existing[3],
                    "public_key": existing[4],
                    "reused": True,
                }
            self._append_event(
                "ReleaseRefused",
                cohort_id,
                {
                    "reason": "PROTOCOL_VIOLATION",
                    "detail": "batch already released with different inputs",
                    "stored_batch_id": existing[0],
                },
            )
            raise SealedError("PROTOCOL_VIOLATION", "batch already released with different inputs")

        returns_k = [value / capital for value in control_series]
        p_k = studentized_bootstrap_p(returns_k, b=b, seed=seed + 1).p_upper
        decision_row = self.db.execute(
            "SELECT decision_json,signature FROM decisions WHERE cohort_id=?",
            (cohort_id,),
        ).fetchone()
        decision_hash = None
        if decision_row is not None:
            decision_body = json.loads(decision_row[0])
            decision_payload = {
                **decision_body,
                "signature": decision_row[1],
                "public_key": self.public_key_hex(),
            }
            decision_matches = bool(
                self.verify(decision_payload)
                and decision_body.get("consumption_receipt") == consumed[1]
                and int(decision_body.get("b", -1)) == int(b)
                and int(decision_body.get("seed", -1)) == int(seed)
                and math.isclose(
                    float(decision_body.get("p_control", -1.0)),
                    float(p_k),
                    rel_tol=1e-12,
                    abs_tol=1e-15,
                )
            )
            if not decision_matches:
                self._append_event(
                    "ReleaseRefused",
                    cohort_id,
                    {
                        "reason": "TAMPERED_DECISION",
                        "detail": "stored TASK-14 decision does not match the frozen release inputs",
                    },
                )
                raise SealedError(
                    "TAMPERED_DECISION",
                    "stored TASK-14 decision does not match the frozen release inputs",
                )
            decision_hash = hash_jcs(decision_body)
        redacted: list[dict] = []
        pvals: list[float] = []
        for item in normalized:
            candidate_id = item["candidate_id"]
            valid = bool(item["valid"])
            candidate_hash = item["candidate_hash"]
            daily = item["series"]
            if not valid:
                pvals.append(1.0)
                redacted.append(
                    {
                        "candidate_id": candidate_id,
                        "candidate_hash": candidate_hash,
                        "valid": False,
                        "verdict": "INVALID",
                        "p_profit": 1.0,
                        "p_control": 1.0,
                        "p_iut": 1.0,
                        "mean_daily_return": None,
                        "ci_low": None,
                        "ci_high": None,
                    }
                )
                continue
            returns_c = [value / capital for value in daily]
            p_c = studentized_bootstrap_p(returns_c, b=b, seed=seed).p_upper
            p = iut(p_c, p_k)
            mean = sum(returns_c) / len(returns_c)
            se = studentized_bootstrap_p(returns_c, b=b, seed=seed).se
            ci_low = mean - 1.96 * se
            ci_high = mean + 1.96 * se
            pvals.append(p)
            redacted.append(
                {
                    "candidate_id": candidate_id,
                    "candidate_hash": candidate_hash,
                    "valid": True,
                    "verdict": classify(p, power, ci_low, ci_high, economic_minimum),
                    "p_profit": p_c,
                    "p_control": p_k,
                    "p_iut": p,
                    "mean_daily_return": mean,
                    "ci_low": ci_low,
                    "ci_high": ci_high,
                }
            )
        alpha = cohort[4] / cohort[5]
        significant = holm(pvals, alpha)
        adjusted = self._holm_adjusted(pvals)
        for index, item in enumerate(redacted):
            item["p_holm"] = adjusted[index]
            item["holm_significant"] = bool(significant[index])

        derived = {
            "G1": {"lineage.valid": lineage_bound},
            "G4": {
                "cohort.frozen_before_access": self._frozen_before_access(cohort[3]),
                "window.valid_uncontaminated_spent_receipt": bool(
                    self.evaluation_allowed(cohort_id)["allowed"]
                    and data_status["verified"]
                    and not data_status["contaminated"]
                ),
                "holm.conservative_p_rejected_null": bool(any(significant)),
                "economic_effect.both_minima_pass": bool(
                    any(
                        item["valid"]
                        and item["ci_low"] is not None
                        and item["ci_low"] > economic_minimum
                        and item["mean_daily_return"] > economic_minimum
                        for item in redacted
                    )
                ),
            },
        }
        gate_result = evaluate_gates(gate_evidence, derived)
        promotion_gate = bool(
            gate_result["all_pass"] and lineage_bound and data_status["verified"]
        )
        for item in redacted:
            item["promoted"] = bool(
                promotion_gate and item["valid"] and item["verdict"] == "EDGE"
            )
        promotion_allowed = any(item["promoted"] for item in redacted)
        synthetic = bool(data_status.get("synthetic", False))
        scope = {
            "cohort_id": cohort_id,
            "program_id": lineage["program_id"],
            "cohort_index": lineage["cohort_index"],
            "window_commitment": cohort[3],
            "alpha_num": cohort[4],
            "alpha_den": cohort[5],
            "capital": float(capital),
            "economic_minimum": float(economic_minimum),
            "b": int(b),
            "seed": int(seed),
            "power": float(power),
            "family_size": len(redacted),
            "candidate_ids": candidate_ids,
            "synthetic": synthetic,
        }
        evidence_hashes = {
            **lineage,
            "control_hash": control_hash,
            "released_candidate_run_keys_hash": candidate_keys_hash,
            "gate_evidence_hash": gate_evidence_hash,
            "data_manifest_hash": data_status["manifest_hash"],
        }
        manifest = {
            "cohort_id": cohort_id,
            "window_commitment": cohort[3],
            "consumption_receipt": consumed[1],
            "control_hash": control_hash,
            "family_size": len(redacted),
            "alpha_num": cohort[4],
            "alpha_den": cohort[5],
            "capital": float(capital),
            "economic_minimum": float(economic_minimum),
            "b": int(b),
            "seed": int(seed),
            "power": float(power),
            "candidates": redacted,
            "invalid_in_family": sum(1 for item in redacted if not item["valid"]),
            "family_size": len(redacted),
            "scope_hash": hash_jcs(scope),
            "evidence_hash": hash_jcs(evidence_hashes),
            "gates": gate_result,
            "promotion_allowed": promotion_allowed,
            "promoted_candidates": [
                item["candidate_id"] for item in redacted if item["promoted"]
            ],
            "lineage_bound": lineage_bound,
            "data_evidence": data_status,
            "certification": {
                "synthetic_cohort": synthetic,
                "market_result": False,
                "fresh_oos_confirmation": "EXTERNAL_BLOCKED",
                "g4_market_confirmation": (
                    "EXTERNAL_BLOCKED" if synthetic else "PENDING_INDEPENDENT_AUDIT"
                ),
            },
            "raw_inputs_released": False,
            "no_early_visibility": True,
        }
        body = {
            "batch_id": batch_id,
            "cohort_id": cohort_id,
            "program_id": lineage["program_id"],
            "window_commitment": cohort[3],
            "consumption_receipt": consumed[1],
            "input_hash": input_hash,
            "scope": scope,
            "scope_hash": hash_jcs(scope),
            "evidence_hashes": evidence_hashes,
            "evidence_hash": hash_jcs(evidence_hashes),
            "gate_evidence_hash": gate_evidence_hash,
            "data_manifest_hash": data_status["manifest_hash"],
            "candidate_run_keys_hash": candidate_keys_hash,
            "gates": gate_result,
            "promotion_allowed": promotion_allowed,
            "task14": {
                "method": "TASK14_PAIRED_IUT_HOLM_V1",
                "family_size": len(redacted),
                "alpha_num": cohort[4],
                "alpha_den": cohort[5],
                "holm_rejections": sum(1 for value in significant if value),
                "paired_decision_hash": decision_hash,
                "paired_decision_verified": decision_row is not None,
            },
            "manifest": manifest,
            "request_id": request_id,
            "created_ns": _now(),
        }
        signature = self._key().sign(canonical_bytes(body)).hex()
        public_key = self.public_key_hex()
        try:
            self.db.execute(
                "INSERT INTO batches VALUES(?,?,?,?,?,?,?)",
                (
                    batch_id,
                    cohort_id,
                    input_hash,
                    json.dumps(body, sort_keys=True),
                    signature,
                    public_key,
                    _now(),
                ),
            )
            self.db.execute("UPDATE cohorts SET state='RELEASED' WHERE cohort_id=?", (cohort_id,))
            self.db.commit()
        except sqlite3.IntegrityError as exc:
            self.db.rollback()
            raise SealedError("PROTOCOL_VIOLATION", "batch already released") from exc
        self._append_event(
            "BatchReleased",
            cohort_id,
            {
                "batch_id": batch_id,
                "input_hash": input_hash,
                "scope_hash": body["scope_hash"],
                "evidence_hash": body["evidence_hash"],
                "promotion_allowed": promotion_allowed,
                "gate_status": gate_result["status"],
                "synthetic": synthetic,
            },
        )
        return {**body, "signature": signature, "public_key": public_key}

    def export_release_manifest(self, cohort_id: str) -> dict:
        row = self.db.execute(
            "SELECT batch_json,signature,public_key FROM batches WHERE cohort_id=?",
            (cohort_id,),
        ).fetchone()
        if row is None:
            raise SealedError("WAITING_AUTHORIZED_COHORT", "no released batch")
        body = json.loads(row[0])
        return {
            **body["manifest"],
            "batch_id": body["batch_id"],
            "input_hash": body["input_hash"],
            "program_id": body.get("program_id"),
            "scope": body.get("scope"),
            "scope_hash": body.get("scope_hash"),
            "evidence_hashes": body.get("evidence_hashes"),
            "evidence_hash": body.get("evidence_hash"),
            "gates": body.get("gates"),
            "promotion_allowed": body.get("promotion_allowed"),
            "task14": body.get("task14"),
            "signature": row[1],
            "public_key": row[2],
        }

    def verify_release_batch(
        self,
        batch: dict,
        trusted_keys: set[str] | list[str] | dict | None = None,
        *,
        seen_batch_ids: set[str] | list[str] | None = None,
        expected_input_hash: str | None = None,
        raw_candidates: list[dict] | None = None,
    ) -> dict:
        try:
            body = {k: value for k, value in batch.items() if k not in ("signature", "public_key")}
            public_key = batch["public_key"]
            signature = batch["signature"]
        except KeyError:
            return {"valid": False, "reason": "MALFORMED_BATCH", "batch_id": None}
        # The registry is the root of trust. A caller may narrow the accepted
        # key set, but can never add a key the registry does not already trust.
        registry_trusted = self.trusted_keys()
        if trusted_keys is None:
            trusted = registry_trusted
        else:
            trusted = registry_trusted & set(trusted_keys)
        if public_key not in trusted:
            return {"valid": False, "reason": "UNREGISTERED_KEY", "batch_id": body.get("batch_id")}
        try:
            pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key))
            pub.verify(bytes.fromhex(signature), canonical_bytes(body))
        except Exception:
            return {"valid": False, "reason": "INVALID_SIGNATURE", "batch_id": body.get("batch_id")}
        manifest = body.get("manifest")
        if not isinstance(manifest, dict):
            return {"valid": False, "reason": "MALFORMED_BATCH", "batch_id": body.get("batch_id")}

        required = (
            "batch_id",
            "cohort_id",
            "program_id",
            "window_commitment",
            "consumption_receipt",
            "input_hash",
            "scope",
            "scope_hash",
            "evidence_hashes",
            "evidence_hash",
            "gate_evidence_hash",
            "data_manifest_hash",
            "candidate_run_keys_hash",
            "gates",
            "promotion_allowed",
            "task14",
            "manifest",
            "created_ns",
        )
        missing = [field for field in required if field not in body]
        if missing:
            return {
                "valid": False,
                "reason": "MALFORMED_BATCH",
                "batch_id": body.get("batch_id"),
                "missing": missing,
            }
        from .gates import gate_result_is_complete

        if not gate_result_is_complete(body.get("gates")):
            return {
                "valid": False,
                "reason": "MISSING_GATE_FIELDS",
                "batch_id": body.get("batch_id"),
            }
        scope = body.get("scope")
        if not isinstance(scope, dict):
            return {"valid": False, "reason": "MALFORMED_BATCH", "batch_id": body.get("batch_id")}
        if body.get("scope_hash") != hash_jcs(scope):
            return {"valid": False, "reason": "TAMPERED_BATCH", "batch_id": body.get("batch_id")}
        evidence_hashes = body.get("evidence_hashes")
        if not isinstance(evidence_hashes, dict):
            return {"valid": False, "reason": "MALFORMED_BATCH", "batch_id": body.get("batch_id")}
        if body.get("evidence_hash") != hash_jcs(evidence_hashes):
            return {"valid": False, "reason": "TAMPERED_BATCH", "batch_id": body.get("batch_id")}
        if scope.get("cohort_id") != body.get("cohort_id"):
            return {"valid": False, "reason": "TAMPERED_BATCH", "batch_id": body.get("batch_id")}
        if scope.get("window_commitment") != body.get("window_commitment"):
            return {"valid": False, "reason": "TAMPERED_BATCH", "batch_id": body.get("batch_id")}

        cohort = self._cohort_row(body.get("cohort_id"))
        if cohort is None:
            return {"valid": False, "reason": "INVALID_LINEAGE", "batch_id": body.get("batch_id")}
        try:
            lineage = self._lineage_evidence(cohort)
        except SealedError:
            return {"valid": False, "reason": "INVALID_LINEAGE", "batch_id": body.get("batch_id")}
        for field in (
            "cohort_id",
            "program_id",
            "window_commitment",
            "protocol_hash",
            "split_hash",
            "verifier_binary_hash",
            "certificate_hashes_hash",
        ):
            if evidence_hashes.get(field) != lineage.get(field):
                return {
                    "valid": False,
                    "reason": "INVALID_LINEAGE",
                    "batch_id": body.get("batch_id"),
                }
        if evidence_hashes.get("control_hash") != manifest.get("control_hash"):
            return {"valid": False, "reason": "TAMPERED_BATCH", "batch_id": body.get("batch_id")}
        if body.get("gate_evidence_hash") != evidence_hashes.get("gate_evidence_hash"):
            return {"valid": False, "reason": "TAMPERED_BATCH", "batch_id": body.get("batch_id")}
        if body.get("data_manifest_hash") != evidence_hashes.get("data_manifest_hash"):
            return {"valid": False, "reason": "TAMPERED_BATCH", "batch_id": body.get("batch_id")}
        if body.get("candidate_run_keys_hash") != evidence_hashes.get(
            "released_candidate_run_keys_hash"
        ):
            return {"valid": False, "reason": "TAMPERED_BATCH", "batch_id": body.get("batch_id")}

        consumed = self._consumption_row(body.get("cohort_id"))
        anchor_record = self.anchor.latest_consumption(body.get("window_commitment"))
        if (
            consumed is None
            or consumed[1] != body.get("consumption_receipt")
            or anchor_record is None
            or anchor_record.get("payload", {}).get("receipt_hash")
            != body.get("consumption_receipt")
        ):
            return {
                "valid": False,
                "reason": "INVALID_CONSUMPTION_RECEIPT",
                "batch_id": body.get("batch_id"),
            }

        candidates = manifest.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            return {"valid": False, "reason": "MALFORMED_BATCH", "batch_id": body.get("batch_id")}
        if manifest.get("raw_inputs_released") is not False:
            return {
                "valid": False,
                "reason": "RAW_INPUTS_RELEASED",
                "batch_id": body.get("batch_id"),
            }
        for candidate in candidates:
            if not isinstance(candidate, dict) or "daily" in candidate:
                return {
                    "valid": False,
                    "reason": "RAW_INPUTS_RELEASED",
                    "batch_id": body.get("batch_id"),
                }
            if candidate.get("valid") is False and (
                candidate.get("p_iut") != 1.0 or candidate.get("p_holm") != 1.0
            ):
                return {
                    "valid": False,
                    "reason": "INVALID_CANDIDATE_FAMILY",
                    "batch_id": body.get("batch_id"),
                }
        if scope.get("family_size") != len(candidates):
            return {"valid": False, "reason": "TAMPERED_BATCH", "batch_id": body.get("batch_id")}
        if scope.get("candidate_ids") != [item.get("candidate_id") for item in candidates]:
            return {"valid": False, "reason": "TAMPERED_BATCH", "batch_id": body.get("batch_id")}
        if body.get("promotion_allowed") != manifest.get("promotion_allowed"):
            return {"valid": False, "reason": "TAMPERED_BATCH", "batch_id": body.get("batch_id")}
        if bool(body.get("promotion_allowed")) != any(
            item.get("promoted") is True for item in candidates
        ):
            return {"valid": False, "reason": "TAMPERED_BATCH", "batch_id": body.get("batch_id")}
        if any(
            item.get("promoted") is True
            and (
                body["gates"].get("all_pass") is not True
                or manifest.get("lineage_bound") is not True
                or manifest.get("data_evidence", {}).get("verified") is not True
                or item.get("verdict") != "EDGE"
            )
            for item in candidates
        ):
            return {"valid": False, "reason": "PROMOTION_GATE_BYPASS", "batch_id": body.get("batch_id")}
        if manifest.get("certification", {}).get("market_result") is not False:
            return {"valid": False, "reason": "TAMPERED_BATCH", "batch_id": body.get("batch_id")}
        if body.get("task14", {}).get("family_size") != len(candidates):
            return {"valid": False, "reason": "TAMPERED_BATCH", "batch_id": body.get("batch_id")}

        try:
            recomputed = self._batch_input_hash(
                body["cohort_id"],
                body["window_commitment"],
                body["consumption_receipt"],
                manifest["control_hash"],
                manifest["candidates"],
                manifest["capital"],
                manifest["economic_minimum"],
                manifest["b"],
                manifest["seed"],
                manifest["power"],
                gate_evidence_hash=body.get("gate_evidence_hash"),
                data_manifest_hash=body.get("data_manifest_hash"),
                candidate_run_keys_hash=body.get("candidate_run_keys_hash"),
            )
        except (KeyError, TypeError, ValueError):
            return {"valid": False, "reason": "MALFORMED_BATCH", "batch_id": body.get("batch_id")}
        if recomputed != body.get("input_hash"):
            return {"valid": False, "reason": "TAMPERED_BATCH", "batch_id": body.get("batch_id")}
        if expected_input_hash is not None and expected_input_hash != body["input_hash"]:
            return {"valid": False, "reason": "CHANGED_INPUTS", "batch_id": body.get("batch_id")}
        if raw_candidates is not None:
            try:
                supplied = [
                    self._candidate_hash(candidate["daily"]) for candidate in raw_candidates
                ]
            except (KeyError, TypeError, ValueError):
                return {
                    "valid": False,
                    "reason": "CHANGED_INPUTS",
                    "batch_id": body.get("batch_id"),
                }
            expected = [item["candidate_hash"] for item in manifest["candidates"]]
            if supplied != expected:
                return {
                    "valid": False,
                    "reason": "CHANGED_INPUTS",
                    "batch_id": body.get("batch_id"),
                }
        if seen_batch_ids is not None and body.get("batch_id") in set(seen_batch_ids):
            return {
                "valid": False,
                "reason": "DUPLICATE_RELEASE",
                "batch_id": body.get("batch_id"),
            }
        stored = self.db.execute(
            "SELECT batch_id FROM batches WHERE cohort_id=?", (body.get("cohort_id"),)
        ).fetchone()
        if stored is not None and stored[0] != body.get("batch_id"):
            return {
                "valid": False,
                "reason": "DUPLICATE_RELEASE",
                "batch_id": body.get("batch_id"),
            }
        return {
            "valid": True,
            "reason": None,
            "batch_id": body.get("batch_id"),
            "manifest": manifest,
        }

    # ------------------------------------------------------------- isolation
    def isolation_report(self) -> dict:
        def mode(path: Path) -> str:
            return oct(stat.S_IMODE(os.stat(path).st_mode))

        qel_root = Path(os.environ.get("QEL_ROOT", os.getcwd())).resolve()
        sealed = self.root.resolve()
        anchor = self.anchor.root.resolve()
        research_roots = [(qel_root / name).resolve() for name in RESEARCH_ARTIFACT_DIRS]
        sealed_in_research = any(
            sealed == item or sealed.is_relative_to(item) for item in research_roots
        )
        anchor_in_research = any(
            anchor == item or anchor.is_relative_to(item) for item in research_roots
        )
        api_methods = sorted(
            name
            for name in dir(self)
            if not name.startswith("_") and callable(getattr(self, name, None))
        )
        path_reading = [
            name
            for name in api_methods
            if name.startswith(("read_", "open_", "load_", "readfile", "openfile"))
        ]
        checks = {
            "sealed_root_mode_0700": mode(self.root) == "0o700",
            "db_mode_0600": mode(self.db_path) == "0o600",
            "key_mode_0600": mode(self.key_path) == "0o600",
            "anchor_root_mode_0700": mode(self.anchor.root) == "0o700",
            "anchor_log_mode_0600": mode(self.anchor.path) == "0o600",
            "anchor_outside_sealed_root": not (
                anchor == sealed or anchor.is_relative_to(sealed)
            ),
            "sealed_database_outside_research_artifacts": not sealed_in_research,
            "anchor_outside_research_artifacts": not anchor_in_research,
            "no_path_reading_api": not path_reading,
            "sibling_outputs_not_exposed": not path_reading,
        }
        return {
            "status": "PASS_SOFTWARE_ONLY" if all(checks.values()) else "FAIL",
            "checks": checks,
            "modes": {
                "sealed_root": mode(self.root),
                "db": mode(self.db_path),
                "key": mode(self.key_path),
                "anchor_root": mode(self.anchor.root),
                "anchor_log": mode(self.anchor.path),
            },
            "paths": {
                "sealed_root": str(sealed),
                "db": str(self.db_path.resolve()),
                "key": str(self.key_path.resolve()),
                "anchor_root": str(anchor),
                "anchor_log": str(self.anchor.path.resolve()),
            },
            "api_methods": api_methods,
            "path_reading_api_methods": path_reading,
            "os_separation": {
                "status": "BLOCKED",
                "verified": False,
                "missing_capability": (
                    "administrator-created Unix principals qel-research and qel-eval "
                    "(useradd/root), privileged socket ownership changes, and a network "
                    "namespace or firewall for the worker"
                ),
                "blocked_checks": [
                    "actual distinct Linux UIDs for research and evaluator",
                    "socket peer credentials and authorized-socket ownership",
                    "worker with no network access",
                    "mount/namespace separation for sibling research outputs",
                ],
                "not_claimed": [
                    "uid_separation",
                    "socket_isolation",
                    "worker_network_isolation",
                ],
            },
            "anchor_chain": self.anchor.verify_chain(),
        }


def verify_release_batch(
    batch: dict,
    registry: SealedRegistry,
    trusted_keys: set[str] | list[str] | dict | None = None,
    **kwargs,
) -> dict:
    return registry.verify_release_batch(batch, trusted_keys, **kwargs)


def f25_scenario(root: str) -> dict:
    reg = SealedRegistry(root)
    reg.freeze_cohort("COHORT-0", "PROG-LAB", 0, ["a" * 64], "b" * 64, "c" * 64, "d" * 64, ["e" * 64])
    spend = reg.spend_consumption("COHORT-0", acknowledged=True)
    retry = reg.spend_consumption("COHORT-0", acknowledged=True)
    new_candidate = False
    try:
        reg.freeze_cohort("COHORT-1", "PROG-LAB", 0, ["f" * 64], "b" * 64, "c" * 64, "d" * 64, ["e" * 64])
    except SealedError as exc:
        new_candidate = exc.code in (
            "WAITING_FRESH_OOS",
            "PROTOCOL_VIOLATION",
            "WAITING_AUTHORIZED_COHORT",
        )
    return {
        "consumption_count": 1 if not spend["reused"] else 0,
        "alpha_spent": f'{spend["alpha_spent_num"] / spend["alpha_spent_den"]:.6f}'.rstrip("0").rstrip("."),
        "retry_alpha_spend": 0 if retry["reused"] else 1,
        "new_candidate_denied": new_candidate,
        "receipt_anchored_before_read": spend["anchor_hash"] != "",
    }


def f26_scenario(root: str) -> dict:
    reg = SealedRegistry(root)
    reg.lease("LEASE-1", 1)
    reg.lease("LEASE-1", 2)
    stale = reg.stale_result("LEASE-1", 1)
    duplicate = reg.reserve_trial("TRIAL-1", "COHORT-0")
    duplicate2 = reg.reserve_trial("TRIAL-1", "COHORT-0")
    restored = reg.restore_from_old(0, 2)
    return {
        "stale_result_committed": not stale,
        "duplicate_trial_reserved": duplicate2,
        "sealed_state": restored["state"],
        "alpha_counter_decreased": restored["alpha_counter_decreased"],
        "first_reservation": duplicate,
    }
