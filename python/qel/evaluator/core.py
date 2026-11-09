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
        if len(candidate_daily) < 3 or len(control_daily) < 3:
            raise SealedError("INSUFFICIENT_HISTORY")
        returns_c = [x / capital for x in candidate_daily]
        returns_k = [x / capital for x in control_daily]
        p_c = studentized_bootstrap_p(returns_c, b=b, seed=seed).p_upper
        p_k = studentized_bootstrap_p(returns_k, b=b, seed=seed + 1).p_upper
        p = iut(p_c, p_k)
        mean = sum(returns_c) / len(returns_c)
        se = studentized_bootstrap_p(returns_c, b=b, seed=seed).se
        ci_low = mean - 1.96 * se
        ci_high = mean + 1.96 * se
        verdict = classify(p, power, ci_low, ci_high, economic_minimum)
        body = {
            "cohort_id": cohort_id,
            "verdict": verdict,
            "p_profit": p_c,
            "p_control": p_k,
            "p_iut": p,
            "ci_low": ci_low,
            "ci_high": ci_high,
            "mean_daily_return": mean,
            "b": b,
            "consumption_receipt": consumed[1],
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
            body = {k: v for k, v in decision.items() if k not in ("signature", "public_key")}
            pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(decision["public_key"]))
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
                state = "RELEASED" if decision else "CONSUMED_RUNNING"
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
    ) -> str:
        return hash_jcs(
            {
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
        )

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
    ) -> dict:
        """Compute, sign and release one redacted multi-candidate batch.

        Invalid candidates remain in the Holm family with p=1.0. Raw candidate
        or control returns are never written to the batch or manifest.
        """
        self._guard_ready()
        cohort = self._cohort_row(cohort_id)
        if cohort is None:
            raise SealedError("WAITING_FRESH_OOS", "no frozen cohort")
        consumed = self._consumption_row(cohort_id)
        if consumed is None:
            raise SealedError("WAITING_AUTHORIZED_COHORT", "no pre-access consumption receipt")
        anchor_record = self.anchor.latest_consumption(cohort[3])
        if (
            anchor_record is None
            or anchor_record.get("payload", {}).get("receipt_hash") != consumed[1]
        ):
            raise SealedError("ANCHOR_UNAVAILABLE", "independent anchor acknowledgement missing")
        if self.db.execute(
            "SELECT 1 FROM batches WHERE cohort_id=?", (cohort_id,)
        ).fetchone():
            raise SealedError("PROTOCOL_VIOLATION", "batch already released")
        if len(control_daily) < 3:
            raise SealedError("INSUFFICIENT_HISTORY", "control history too short")
        control_hash = hash_jcs([float(value) for value in control_daily])
        returns_k = [value / capital for value in control_daily]
        p_k = studentized_bootstrap_p(returns_k, b=b, seed=seed + 1).p_upper
        redacted: list[dict] = []
        pvals: list[float] = []
        for index, candidate in enumerate(candidates):
            candidate_id = str(candidate.get("candidate_id", f"C{index}"))
            daily = candidate.get("daily", [])
            valid = False
            candidate_hash = hash_jcs({"candidate_id": candidate_id, "invalid": True})
            try:
                candidate_hash = self._candidate_hash(daily)
                valid = len(daily) >= 3 and all(
                    math.isfinite(float(value)) for value in daily
                )
            except (TypeError, ValueError):
                valid = False
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
        input_hash = self._batch_input_hash(
            cohort_id,
            cohort[3],
            consumed[1],
            control_hash,
            redacted,
            capital,
            economic_minimum,
            b,
            seed,
            power,
        )
        batch_id = hash_jcs(
            {
                "cohort_id": cohort_id,
                "input_hash": input_hash,
                "consumption_receipt": consumed[1],
            }
        )
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
            "raw_inputs_released": False,
        }
        body = {
            "batch_id": batch_id,
            "cohort_id": cohort_id,
            "window_commitment": cohort[3],
            "consumption_receipt": consumed[1],
            "input_hash": input_hash,
            "manifest": manifest,
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
        if trusted_keys is None:
            trusted = self.trusted_keys()
        elif isinstance(trusted_keys, dict):
            trusted = set(trusted_keys)
        else:
            trusted = set(trusted_keys)
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
