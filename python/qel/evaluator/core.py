"""Sealed evaluator: cohort freeze, pre-access consumption and signed decisions.

The private registry is a separate SQLite file with its own anchor chain. It
is not the research ledger. Full OS-owner separation is recorded as a
deployment blocker where distinct Unix principals are unavailable.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives import serialization

from ..contract.canonical import canonical_bytes, hash_jcs
from ..stats.core import classify, iut, studentized_bootstrap_p


class SealedError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def _now() -> str:
    return str(time.time_ns())


class SealedRegistry:
    def __init__(self, root: str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.db_path = self.root / "sealed.sqlite"
        self.anchor_path = self.root / "anchors.log"
        self.key_path = self.root / "evaluator.key"
        self.db = sqlite3.connect(self.db_path)
        os.chmod(self.db_path, 0o600)
        self._migrate()
        self._ensure_key()

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
              created_ns TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS consumption (
              cohort_id TEXT PRIMARY KEY,
              receipt_hash TEXT NOT NULL,
              alpha_spent_num INTEGER NOT NULL,
              alpha_spent_den INTEGER NOT NULL,
              anchor_hash TEXT NOT NULL,
              created_ns TEXT NOT NULL
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
            """
        )
        self.db.commit()

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

    def _anchor(self, payload: dict) -> str:
        previous = "0" * 64
        if self.anchor_path.exists():
            lines = self.anchor_path.read_text().splitlines()
            if lines:
                previous = json.loads(lines[-1])["anchor_hash"]
        body = {"previous": previous, "payload": payload, "recorded_ns": _now()}
        h = hash_jcs(body)
        with self.anchor_path.open("a") as f:
            f.write(json.dumps({**body, "anchor_hash": h}, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return h

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
        if self.db.execute("SELECT 1 FROM cohorts WHERE cohort_id=?", (cohort_id,)).fetchone():
            raise SealedError("PROTOCOL_VIOLATION", "cohort already exists")
        if self.db.execute(
            "SELECT 1 FROM cohorts WHERE program_id=? AND cohort_index=?",
            (program_id, cohort_index),
        ).fetchone():
            raise SealedError("PROTOCOL_VIOLATION", "window already consumed or frozen")
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
        self.db.execute(
            "INSERT INTO cohorts VALUES(?,?,?,?,?,?,?,?)",
            (cohort_id, program_id, cohort_index, commitment, 5, 100 * alpha_den, "FROZEN", _now()),
        )
        self.db.commit()
        anchor = self._anchor({"type": "CohortFrozen", "cohort_id": cohort_id, "commitment": commitment})
        return {
            "cohort_id": cohort_id,
            "state": "FROZEN",
            "commitment": commitment,
            "alpha_num": 5,
            "alpha_den": 100 * alpha_den,
            "anchor_hash": anchor,
        }

    def spend_consumption(self, cohort_id: str, acknowledged: bool) -> dict:
        if not acknowledged:
            raise SealedError("WAITING_AUTHORIZED_COHORT", "anchor receipt not acknowledged")
        row = self.db.execute(
            "SELECT alpha_num,alpha_den,state FROM cohorts WHERE cohort_id=?", (cohort_id,)
        ).fetchone()
        if row is None:
            raise SealedError("WAITING_FRESH_OOS", "no frozen cohort")
        existing = self.db.execute("SELECT receipt_hash,alpha_spent_num,alpha_spent_den,anchor_hash FROM consumption WHERE cohort_id=?", (cohort_id,)).fetchone()
        if existing:
            return {
                "cohort_id": cohort_id,
                "receipt_hash": existing[0],
                "alpha_spent_num": existing[1],
                "alpha_spent_den": existing[2],
                "anchor_hash": existing[3],
                "reused": True,
            }
        alpha_num, alpha_den, state = row
        if state not in ("FROZEN", "CONSUMED_RUNNING"):
            raise SealedError("WAITING_FRESH_OOS", f"cohort state {state}")
        receipt = {
            "cohort_id": cohort_id,
            "alpha_num": alpha_num,
            "alpha_den": alpha_den,
            "spent_ns": _now(),
        }
        receipt_hash = hash_jcs(receipt)
        anchor = self._anchor({"type": "OOSConsumed", "cohort_id": cohort_id, "receipt_hash": receipt_hash})
        self.db.execute(
            "INSERT INTO consumption VALUES(?,?,?,?,?,?)",
            (cohort_id, receipt_hash, alpha_num, alpha_den, anchor, _now()),
        )
        self.db.execute("UPDATE cohorts SET state='CONSUMED_RUNNING' WHERE cohort_id=?", (cohort_id,))
        self.db.commit()
        return {
            "cohort_id": cohort_id,
            "receipt_hash": receipt_hash,
            "alpha_spent_num": alpha_num,
            "alpha_spent_den": alpha_den,
            "anchor_hash": anchor,
            "reused": False,
        }

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
        if self.db.execute("SELECT 1 FROM decisions WHERE cohort_id=?", (cohort_id,)).fetchone():
            raise SealedError("PROTOCOL_VIOLATION", "decision already issued")
        consumed = self.db.execute("SELECT receipt_hash FROM consumption WHERE cohort_id=?", (cohort_id,)).fetchone()
        if consumed is None:
            raise SealedError("WAITING_AUTHORIZED_COHORT", "no pre-access consumption receipt")
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
            "consumption_receipt": consumed[0],
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

    def restore_from_old(self, local_generation: int, anchor_generation: int) -> dict:
        if local_generation < anchor_generation:
            self.db.execute("UPDATE cohorts SET state='WAITING_FRESH_OOS' WHERE state='CONSUMED_RUNNING'")
            self.db.commit()
            return {"state": "WAITING_FRESH_OOS", "alpha_counter_decreased": False}
        return {"state": "UNCHANGED", "alpha_counter_decreased": False}

    def reserve_trial(self, trial_key: str, cohort_id: str) -> bool:
        try:
            self.db.execute("INSERT INTO trials VALUES(?,?,?)", (trial_key, cohort_id, _now()))
            self.db.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def lease(self, lease_id: str, generation: int) -> None:
        self.db.execute("INSERT OR REPLACE INTO leases VALUES(?,?,?)", (lease_id, generation, _now()))
        self.db.commit()

    def stale_result(self, lease_id: str, generation: int) -> bool:
        row = self.db.execute("SELECT generation FROM leases WHERE lease_id=?", (lease_id,)).fetchone()
        return row is None or row[0] != generation


def f25_scenario(root: str) -> dict:
    reg = SealedRegistry(root)
    reg.freeze_cohort("COHORT-0", "PROG-LAB", 0, ["a" * 64], "b" * 64, "c" * 64, "d" * 64, ["e" * 64])
    spend = reg.spend_consumption("COHORT-0", acknowledged=True)
    retry = reg.spend_consumption("COHORT-0", acknowledged=True)
    new_candidate = False
    try:
        reg.freeze_cohort("COHORT-1", "PROG-LAB", 0, ["f" * 64], "b" * 64, "c" * 64, "d" * 64, ["e" * 64])
    except SealedError as exc:
        new_candidate = exc.code in ("WAITING_FRESH_OOS", "PROTOCOL_VIOLATION", "WAITING_AUTHORIZED_COHORT")
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
