"""TASK-15 isolation, independent anchor and signed-batch negative tests.

These tests exercise the software boundaries only. Actual distinct Unix UIDs,
socket peer credentials and worker network isolation require administrator
created principals and are recorded as blocked in ``isolation_report``.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import threading
from fractions import Fraction
from pathlib import Path

import pytest

from qel.evaluator.core import AnchorStore, SealedError, SealedRegistry, verify_release_batch

FREEZE = {
    "program_id": "PROG-T15",
    "cohort_index": 0,
    "candidate_run_keys": ["a" * 64],
    "protocol_hash": "b" * 64,
    "split_hash": "c" * 64,
    "verifier_binary_hash": "d" * 64,
    "certificate_hashes": ["e" * 64],
}

CANDIDATE = [0.002, 0.001, 0.003, 0.002, 0.001, 0.0015]
CONTROL = [0.0, 0.001, -0.001, 0.0, 0.0, 0.0005]


def _freeze(reg: SealedRegistry, cohort_id: str = "COHORT-0") -> dict:
    return reg.freeze_cohort(cohort_id, **FREEZE)


def _spend(reg: SealedRegistry, cohort_id: str = "COHORT-0", **kwargs) -> dict:
    return reg.spend_consumption(cohort_id, acknowledged=True, **kwargs)


def _alpha_total(reg: SealedRegistry) -> Fraction:
    rows = reg.db.execute(
        "SELECT alpha_spent_num,alpha_spent_den FROM consumption"
    ).fetchall()
    return sum((Fraction(num, den) for num, den in rows), Fraction(0, 1))


def test_isolation_modes_and_independent_anchor(tmp_path, monkeypatch):
    monkeypatch.delenv("QEL_ANCHOR_ROOT", raising=False)
    reg = SealedRegistry(str(tmp_path / "sealed"))
    _freeze(reg)
    report = reg.isolation_report()

    assert report["status"] == "PASS_SOFTWARE_ONLY"
    assert all(report["checks"].values()), report["checks"]
    assert report["modes"]["sealed_root"] == "0o700"
    assert report["modes"]["db"] == "0o600"
    assert report["modes"]["key"] == "0o600"
    assert report["modes"]["anchor_root"] == "0o700"
    assert report["modes"]["anchor_log"] == "0o600"
    assert report["path_reading_api_methods"] == []
    assert Path(report["paths"]["anchor_root"]).is_relative_to(tmp_path.resolve())
    assert not Path(report["paths"]["anchor_root"]).is_relative_to(
        Path(report["paths"]["sealed_root"])
    )

    os_separation = report["os_separation"]
    assert os_separation["status"] == "BLOCKED"
    assert os_separation["verified"] is False
    assert "qel-research" in os_separation["missing_capability"]
    assert "uid_separation" in os_separation["not_claimed"]
    assert "socket_isolation" in os_separation["not_claimed"]
    assert "worker_network_isolation" in os_separation["not_claimed"]
    assert report["anchor_chain"]["valid"] is True


def test_anchor_root_env_is_honoured(tmp_path, monkeypatch):
    anchor_root = tmp_path / "env-anchor"
    monkeypatch.setenv("QEL_ANCHOR_ROOT", str(anchor_root))
    reg = SealedRegistry(str(tmp_path / "sealed"))
    assert reg.anchor.root == anchor_root
    assert anchor_root.exists()


def test_anchor_rejects_being_inside_sealed_root(tmp_path):
    sealed = tmp_path / "sealed"
    with pytest.raises(SealedError) as exc:
        SealedRegistry(str(sealed), anchor_root=str(sealed / "anchor"))
    assert exc.value.code == "ISOLATION_VIOLATION"


def test_anchor_chain_is_hash_chained_and_monotonic(tmp_path):
    reg = SealedRegistry(str(tmp_path / "sealed"))
    _freeze(reg)
    _spend(reg)
    records = reg.anchor.records()
    assert [record["generation"] for record in records] == [1, 2]
    assert records[0]["prev_hash"] == "0" * 64
    assert records[1]["prev_hash"] == records[0]["anchor_hash"]
    assert records[0]["payload"]["type"] == "CohortFrozen"
    assert records[1]["payload"]["type"] == "OOSConsumed"
    assert records[1]["alpha_num"] == 5
    assert records[1]["alpha_den"] == 200
    assert records[1]["window_commitment"] == records[0]["window_commitment"]
    assert reg.anchor.verify_chain()["valid"] is True


def test_anchor_chain_detects_tampering(tmp_path):
    reg = SealedRegistry(str(tmp_path / "sealed"))
    _freeze(reg)
    _spend(reg)
    lines = reg.anchor.path.read_text().splitlines()
    record = json.loads(lines[-1])
    record["payload"]["receipt_hash"] = "0" * 64
    lines[-1] = json.dumps(record, sort_keys=True)
    reg.anchor.path.write_text("\n".join(lines) + "\n")
    chain = reg.anchor.verify_chain()
    assert chain["valid"] is False
    assert any("payload_hash" in error for error in chain["errors"])
    with pytest.raises(SealedError) as exc:
        reg.reconcile_against_anchor()
    assert exc.value.code == "ANCHOR_CORRUPT"


def test_pre_access_receipt_commits_before_read(tmp_path):
    reg = SealedRegistry(str(tmp_path / "sealed"))
    frozen = _freeze(reg)

    receipt = reg.prepare_read_receipt("COHORT-0")
    assert receipt["window_commitment"] == frozen["commitment"]
    assert receipt["alpha_num"] == 5
    assert receipt["alpha_den"] == 200
    assert len(receipt["candidate_run_keys_hash"]) == 64
    assert len(receipt["certificate_hashes_hash"]) == 64
    assert receipt["protocol_hash"] == FREEZE["protocol_hash"]
    assert receipt["data_hash"] == FREEZE["split_hash"]
    assert receipt["verifier_binary_hash"] == FREEZE["verifier_binary_hash"]
    assert len(receipt["signature"]) == 128
    assert receipt["receipt_hash"] == frozen["receipt_hash"]

    again = reg.prepare_read_receipt("COHORT-0")
    assert again["receipt_hash"] == receipt["receipt_hash"]
    assert again["prepared_ns"] == receipt["prepared_ns"]

    gate = reg.evaluation_allowed("COHORT-0")
    assert gate["allowed"] is False
    assert gate["reason"] == "WAITING_AUTHORIZED_COHORT"

    _spend(reg)
    gate = reg.evaluation_allowed("COHORT-0")
    assert gate["allowed"] is True
    assert gate["receipt_hash"] == receipt["receipt_hash"]


def test_missing_anchor_ack_blocks_read_and_retry_spends_once(tmp_path):
    anchor = AnchorStore(str(tmp_path / "independent-anchor"))
    reg = SealedRegistry(str(tmp_path / "sealed"), anchor=anchor)
    frozen = _freeze(reg)

    anchor.fail_ack = True
    with pytest.raises(SealedError) as exc:
        _spend(reg)
    assert exc.value.code == "ANCHOR_UNAVAILABLE"
    assert reg.db.execute("SELECT COUNT(*) FROM consumption").fetchone()[0] == 0
    assert reg.evaluation_allowed("COHORT-0")["allowed"] is False
    assert len(reg.anchor.find_by_commitment(frozen["commitment"])) == 2
    with pytest.raises(SealedError):
        reg.decide("COHORT-0", CANDIDATE, CONTROL, capital=1000, economic_minimum=0.0001, b=64)
    assert _alpha_total(reg) == Fraction(0, 1)

    sealed = str(tmp_path / "sealed")
    reg.close()
    anchor.fail_ack = False
    reg = SealedRegistry(sealed, anchor=anchor)
    assert reg.requires_reconcile is True
    recovered = _spend(reg)
    assert recovered["reused"] is False
    assert recovered["recovered"] is True
    assert recovered["receipt_hash"] == frozen["receipt_hash"]
    assert _alpha_total(reg) == Fraction(5, 200)
    consumed_anchor = [
        record
        for record in reg.anchor.records()
        if record["payload"]["type"] == "OOSConsumed"
    ]
    assert len(consumed_anchor) == 1

    retry = _spend(reg)
    assert retry["reused"] is True
    assert retry["receipt_hash"] == recovered["receipt_hash"]
    assert _alpha_total(reg) == Fraction(5, 200)
    assert reg.evaluation_allowed("COHORT-0")["allowed"] is True


def test_concurrent_consumption_has_one_winner(tmp_path):
    sealed = str(tmp_path / "sealed")
    reg = SealedRegistry(sealed)
    frozen = _freeze(reg)
    reg.close()

    barrier = threading.Barrier(2)
    results: list[tuple[str, object]] = []
    lock = threading.Lock()

    def worker(request_id: str) -> None:
        worker_reg = SealedRegistry(sealed)
        try:
            barrier.wait(timeout=10)
            try:
                outcome = _spend(worker_reg, request_id=request_id)
                with lock:
                    results.append(("success", outcome))
            except SealedError as error:
                with lock:
                    results.append(("refused", error.code))
        finally:
            worker_reg.close()

    threads = [
        threading.Thread(target=worker, args=("request-a",)),
        threading.Thread(target=worker, args=("request-b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    successes = [item for item in results if item[0] == "success"]
    refusals = [item for item in results if item[0] == "refused"]
    assert len(successes) == 1, results
    assert len(refusals) == 1, results
    assert refusals[0][1] == "ALREADY_CONSUMED"

    check = SealedRegistry(sealed)
    assert check.db.execute("SELECT COUNT(*) FROM consumption").fetchone()[0] == 1
    assert _alpha_total(check) == Fraction(5, 200)
    consumed = [
        record
        for record in check.anchor.records()
        if record["window_commitment"] == frozen["commitment"]
        and record["payload"]["type"] == "OOSConsumed"
    ]
    assert len(consumed) == 1
    check.close()


def test_old_backup_requires_reconcile_and_keeps_alpha(tmp_path):
    sealed = tmp_path / "sealed"
    anchor_root = str(tmp_path / "shared-anchor")

    reg = SealedRegistry(str(sealed), anchor_root=anchor_root)
    frozen = _freeze(reg)
    reg.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    reg.close()
    old_db = tmp_path / "old.sqlite"
    shutil.copy2(sealed / "sealed.sqlite", old_db)

    reg = SealedRegistry(str(sealed), anchor_root=anchor_root)
    first = _spend(reg)
    reg.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    reg.close()

    shutil.copy2(old_db, sealed / "sealed.sqlite")
    for suffix in ("-wal", "-shm"):
        path = sealed / f"sealed.sqlite{suffix}"
        if path.exists():
            path.unlink()

    restored = SealedRegistry(str(sealed), anchor_root=anchor_root)
    assert restored.requires_reconcile is True
    with pytest.raises(SealedError) as exc:
        _spend(restored)
    assert exc.value.code == "RECONCILE_REQUIRED"
    with pytest.raises(SealedError) as exc:
        _freeze(restored, "COHORT-1")
    assert exc.value.code == "RECONCILE_REQUIRED"
    assert restored.evaluation_allowed("COHORT-0")["allowed"] is False

    reconciled = restored.reconcile_against_anchor()
    assert reconciled["status"] == "RECONCILED"
    assert reconciled["restored_consumptions"] == 1
    assert reconciled["consumption_count"] == 1
    assert Fraction(reconciled["alpha_spent"]) == Fraction(5, 200)
    assert reconciled["alpha_counter_decreased"] is False
    assert restored.requires_reconcile is False

    replay = _spend(restored)
    assert replay["reused"] is True
    assert replay["receipt_hash"] == first["receipt_hash"]
    assert _alpha_total(restored) == Fraction(5, 200)
    assert restored.evaluation_allowed("COHORT-0")["allowed"] is True

    with pytest.raises(SealedError):
        _freeze(restored, "COHORT-2")


def test_copied_registry_root_requires_reconcile(tmp_path):
    original = tmp_path / "sealed"
    anchor_root = str(tmp_path / "shared-anchor")
    reg = SealedRegistry(str(original), anchor_root=anchor_root)
    _freeze(reg)
    reg.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    reg.close()

    copied = tmp_path / "sealed-copy"
    shutil.copytree(original, copied)
    clone = SealedRegistry(str(copied), anchor_root=anchor_root)
    assert clone.requires_reconcile is True
    with pytest.raises(SealedError) as exc:
        _spend(clone)
    assert exc.value.code == "RECONCILE_REQUIRED"
    assert clone.reconcile_against_anchor()["status"] == "RECONCILED"
    fresh = _spend(clone)
    assert fresh["reused"] is False
    assert _alpha_total(clone) == Fraction(5, 200)
    assert _spend(clone)["reused"] is True


def test_copied_consumed_registry_stays_consumed(tmp_path):
    original = tmp_path / "sealed"
    anchor_root = str(tmp_path / "shared-anchor")
    reg = SealedRegistry(str(original), anchor_root=anchor_root)
    frozen = _freeze(reg)
    first = _spend(reg)
    reg.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    reg.close()

    copied = tmp_path / "sealed-copy"
    shutil.copytree(original, copied)
    clone = SealedRegistry(str(copied), anchor_root=anchor_root)
    assert clone.requires_reconcile is True
    with pytest.raises(SealedError) as exc:
        _spend(clone)
    assert exc.value.code == "RECONCILE_REQUIRED"

    reconciled = clone.reconcile_against_anchor()
    assert reconciled["consumption_count"] == 1
    assert Fraction(reconciled["alpha_spent"]) == Fraction(5, 200)
    replay = _spend(clone)
    assert replay["reused"] is True
    assert replay["receipt_hash"] == first["receipt_hash"] == frozen["receipt_hash"]
    assert _alpha_total(clone) == Fraction(5, 200)


def test_renamed_cohort_with_same_commitment_is_refused(tmp_path):
    anchor_root = str(tmp_path / "shared-anchor")
    reg = SealedRegistry(str(tmp_path / "sealed"), anchor_root=anchor_root)
    frozen = _freeze(reg)
    _spend(reg)
    reg.close()

    other = SealedRegistry(str(tmp_path / "other-sealed"), anchor_root=anchor_root)
    with pytest.raises(SealedError) as exc:
        _freeze(other, "COHORT-RENAMED")
    assert exc.value.code == "RECONCILE_REQUIRED"

    other.reconcile_against_anchor()
    with pytest.raises(SealedError) as exc:
        other.freeze_cohort(
            "COHORT-RENAMED",
            program_id=FREEZE["program_id"],
            cohort_index=FREEZE["cohort_index"],
            candidate_run_keys=FREEZE["candidate_run_keys"],
            protocol_hash=FREEZE["protocol_hash"],
            split_hash=FREEZE["split_hash"],
            verifier_binary_hash=FREEZE["verifier_binary_hash"],
            certificate_hashes=FREEZE["certificate_hashes"],
        )
    assert exc.value.code == "PROTOCOL_VIOLATION"

    replay = _spend(other)
    assert replay["reused"] is True
    assert replay["receipt_hash"] == frozen["receipt_hash"]
    assert _alpha_total(other) == Fraction(5, 200)


def test_signed_batch_rejects_unknown_tampered_duplicate_and_changed(tmp_path):
    reg = SealedRegistry(str(tmp_path / "sealed"))
    _freeze(reg)
    _spend(reg)
    reg.trust_key(reg.public_key_hex())

    batch = reg.release_batch(
        "COHORT-0",
        [
            {"candidate_id": "C-A", "daily": CANDIDATE},
            {"candidate_id": "C-B", "daily": [0.1]},
        ],
        CONTROL,
        capital=1000,
        economic_minimum=0.0001,
        b=64,
    )
    manifest = batch["manifest"]
    assert manifest["family_size"] == 2
    assert manifest["invalid_in_family"] == 1
    assert manifest["raw_inputs_released"] is False
    encoded = json.dumps(manifest)
    assert "0.002" not in encoded
    outcomes = {item["candidate_id"]: item for item in manifest["candidates"]}
    assert outcomes["C-B"]["valid"] is False
    assert outcomes["C-B"]["verdict"] == "INVALID"
    assert outcomes["C-B"]["p_iut"] == 1.0
    assert outcomes["C-B"]["p_holm"] == 1.0
    assert outcomes["C-B"]["holm_significant"] is False

    assert reg.verify_release_batch(batch)["valid"] is True
    assert verify_release_batch(batch, reg)["valid"] is True
    exported = reg.export_release_manifest("COHORT-0")
    assert exported["batch_id"] == batch["batch_id"]
    assert exported["family_size"] == 2
    assert "daily" not in exported

    unknown = reg.verify_release_batch(batch, trusted_keys=set())
    assert unknown["reason"] == "UNREGISTERED_KEY"

    tampered = json.loads(json.dumps(batch))
    tampered["manifest"]["candidates"][0]["verdict"] = "EDGE"
    assert reg.verify_release_batch(tampered)["reason"] in (
        "INVALID_SIGNATURE",
        "TAMPERED_BATCH",
    )
    tampered_input = json.loads(json.dumps(batch))
    tampered_input["manifest"]["candidates"][0]["candidate_hash"] = "0" * 64
    assert reg.verify_release_batch(tampered_input)["reason"] in (
        "INVALID_SIGNATURE",
        "TAMPERED_BATCH",
    )

    duplicate = reg.verify_release_batch(batch, seen_batch_ids={batch["batch_id"]})
    assert duplicate["reason"] == "DUPLICATE_RELEASE"
    with pytest.raises(SealedError) as exc:
        reg.release_batch(
            "COHORT-0",
            [{"candidate_id": "C-A", "daily": CANDIDATE}],
            CONTROL,
            capital=1000,
            economic_minimum=0.0001,
            b=64,
        )
    assert exc.value.code == "PROTOCOL_VIOLATION"

    changed = reg.verify_release_batch(batch, expected_input_hash="0" * 64)
    assert changed["reason"] == "CHANGED_INPUTS"
    changed_raw = reg.verify_release_batch(
        batch,
        raw_candidates=[
            {"candidate_id": "C-A", "daily": [9.0, 9.0, 9.0]},
            {"candidate_id": "C-B", "daily": [0.1]},
        ],
    )
    assert changed_raw["reason"] == "CHANGED_INPUTS"


def test_sibling_output_is_not_readable_through_evaluator_api(tmp_path):
    sibling = tmp_path / "research-output.json"
    sibling.write_text(json.dumps({"secret": "SIBLING-SECRET-MARKER"}))
    reg = SealedRegistry(str(tmp_path / "sealed"))
    _freeze(reg)
    report = reg.isolation_report()
    assert report["path_reading_api_methods"] == []
    assert not hasattr(reg, "read_text")
    assert not hasattr(reg, "open")
    with pytest.raises(SealedError):
        reg.export_release_manifest("NO-SUCH-COHORT")
    payload = json.dumps(report, sort_keys=True)
    assert "SIBLING-SECRET-MARKER" not in payload
    assert str(sibling) not in payload
    with sqlite3.connect(reg.db_path) as check:
        tables = {
            row[0]
            for row in check.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "read_receipts" in tables
    assert "batches" in tables
