"""TASK-25 benchmark corpus, evidence and profile-enforcement tests.

These tests do not re-time the benchmarks. They verify that the frozen corpora
match their manifest, that every required target was measured under an enforced
profile, that failures are retained rather than hidden, and that the committed
evidence does not claim full acceptance while B4 100M and the B2/B3/B4 profiles
remain unmeasured.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
CORPUS = ROOT / "benchmarks/corpus"
EVIDENCE = ROOT / "evidence/labd"


def _load_generator():
    spec = importlib.util.spec_from_file_location("qel_gen_corpus", ROOT / "benchmarks/gen_corpus.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def generator():
    return _load_generator()


@pytest.fixture(scope="module")
def manifest():
    return json.loads((CORPUS / "MANIFEST.json").read_text())


@pytest.fixture(scope="module")
def b0():
    path = EVIDENCE / "B0_TYPED_SPECIFICATION.json"
    assert path.exists(), "B0 evidence missing; run benchmarks/run_all_bench.sh and assemble_evidence.py"
    return json.loads(path.read_text())


@pytest.fixture(scope="module")
def b1():
    path = EVIDENCE / "B1_BATCH_SCREEN.json"
    assert path.exists(), "B1 evidence missing"
    return json.loads(path.read_text())


@pytest.fixture(scope="module")
def b5():
    path = EVIDENCE / "B5_LEDGER_RESTORE.json"
    assert path.exists(), "B5 evidence missing"
    return json.loads(path.read_text())


@pytest.fixture(scope="module")
def release():
    path = EVIDENCE / "T_RELEASE_BUILD.json"
    assert path.exists(), "T-RELEASE evidence missing"
    return json.loads(path.read_text())


def test_frozen_corpus_matches_manifest(generator, manifest):
    files = generator.build()
    regenerated = generator.write_manifest(files)
    assert regenerated["files"] == manifest["files"], "corpus bytes drifted from the frozen manifest"
    for name, entry in manifest["files"].items():
        assert hashlib.sha256(files[name]).hexdigest() == entry["sha256"]
        if (CORPUS / name).exists():
            assert (CORPUS / name).read_bytes() == files[name]


def test_b0_corpus_is_256_node_operator_set_1_1(generator):
    policy = generator.build_b0_policy()
    dag = policy["feature_dag"]
    assert len(dag) == 256
    assert policy["operator_set_version"] == "1.1.0"
    operators = {node["operator_id"] for node in dag}
    assert operators <= set(generator.OPERATOR_SET_1_1)
    stateful = {"OP-LAG", "OP-SMA", "OP-SUM", "OP-MIN", "OP-MAX", "OP-EMA", "OP-STD", "OP-ER", "OP-PIVOT", "OP-QUANTILE"}
    assert sum(1 for node in dag if node["operator_id"] in stateful) <= policy["max_stateful_operators"]
    assert policy["entry_policy"]["long_condition_node"] in {n["node_id"] for n in dag}


def test_b1_corpus_matches_declared_generation(generator, manifest):
    assert manifest["counts"]["b1_rows"] == 1_000_000
    assert manifest["counts"]["b1_variants"] == 8
    workload = generator.b1_workload()
    assert workload["variant_thresholds"] == ["1", "1.25", "1.5", "1.75", "2", "2.25", "2.5", "2.75"]
    assert len(workload["common_operators"]) == 8
    assert workload["bar_interval_ns"] == "60000000000"
    rows = list(generator.b1_rows())
    for i in (0, 1, 999, 1000, 123_456, 999_999):
        index, open_, close, high, low, volume = rows[i]
        expected_close = 100_000 + (i % 1000)
        expected_open = 100_000 if i == 0 else 100_000 + ((i - 1) % 1000)
        assert (index, open_, close, high, low, volume) == (
            i,
            expected_open,
            expected_close,
            max(expected_open, expected_close) + 2,
            min(expected_open, expected_close) - 2,
            1000 + i % 97,
        )


def test_b0_evidence_targets_under_enforced_profiles(b0, manifest):
    assert b0["required_check"] == "B0"
    assert b0["corpus"]["sha256"] == manifest["files"]["b0_policy_256.json"]["sha256"]
    assert b0["profiles_enforced"] is True
    for profile in ("RP-4G", "RP-8G"):
        block = b0["profiles"][profile]
        assert block["enforced"] is True
        assert block["enforcement"] == "CGROUP_V2_MEMORY_MAX"
        assert block["declared_memory_max_bytes"] == {"RP-4G": 4 * 2**30, "RP-8G": 8 * 2**30}[profile]
        native = block["native"]
        schema = block["schema_only"]
        assert native["dag_nodes_measured"] == 256
        assert native["native_jit_compilations"] == 0
        assert native["warmups"] == 2
        assert native["timed"]["repeats"] >= 10
        assert native["metric"]["value"] <= 100
        assert schema["metric"]["value"] <= 100
        assert native["failed_attempts"] == []
        assert len(native["timed"]["samples_ms"]) == native["timed"]["repeats"]
        assert block["peaks"]["native_run"]["peak_bytes"] <= block["declared_memory_max_bytes"]
    assert b0["status"] == "PASS"


def test_b1_evidence_targets_under_enforced_profiles(b1, manifest):
    assert b1["required_check"] == "B1"
    assert b1["corpus"]["sha256"] == manifest["files"]["b1_bars.csv"]["sha256"]
    assert b1["corpus"]["rows"] == 1_000_000
    assert b1["corpus"]["variants"] == 8
    assert b1["profiles_enforced"] is True
    assert b1["output_hash_fsync_inside_timed_region"] is True
    for profile in ("RP-4G", "RP-8G"):
        block = b1["profiles"][profile]
        assert block["enforced"] is True
        report = block["report"]
        assert report["warmups"] == 2
        assert report["timed"]["repeats"] >= 10
        assert report["metric"]["value"] <= 10
        assert report["reference_verification"]["status"] == "PASS"
        assert report["phase_p50_ms"]["output_hash_fsync"] > 0
        assert report["failed_attempts"] == []
        assert set(report["include"]) == {"READ", "FEATURES", "VARIANTS", "OUTPUT_HASH_FSYNC"}
        assert len(report["variant_thresholds"]) == 8
        assert len(report["operators_implemented"]) == 8
    assert b1["status"] == "PASS"


def test_b5_evidence_targets_under_enforced_profiles(b5, manifest):
    assert b5["required_check"] == "B5"
    assert b5["corpus"]["sha256"] == manifest["files"]["b5_events.jsonl"]["sha256"]
    assert b5["corpus"]["ledger_events"] == 100_000
    assert b5["corpus"]["artifact_pointers"] == 1_000
    assert b5["profiles_enforced"] is True
    for profile in ("RP-4G", "RP-8G"):
        report = b5["profiles"][profile]["report"]
        assert report["ledger_events_appended"] == 100_000
        assert report["artifact_pointers_sealed"] == 1_000
        assert report["artifact_pointers_referenced"] == 1_000
        assert report["ledger_events_total"] == 101_000
        assert report["restore"]["chain_valid"] is True
        assert report["restore"]["events"] == 101_000
        assert report["crash_matrix"]["report"]["status"] == "PASS"
        trials = report["crash_matrix"]["report"]["trials"]
        assert len(trials) == 6
        assert any(t["acked_events"] > 0 for t in trials), "every crash trial died before any commit; the matrix proves nothing"
        targets = report["targets"]
        assert targets["lost_committed_decisions"]["value"] == 0
        assert targets["torn_pass"]["value"] == 0
        assert targets["read_transaction_ms"]["value"] <= 2000
        assert targets["metadata_restore_minutes"]["value"] <= 60
        assert report["read_transaction"]["repeats"] >= 10
    assert b5["status"] == "PASS"


def test_profiles_are_enforced_not_claimed(b0, b1, b5):
    for evidence in (b0, b1, b5):
        assert evidence["profiles_enforced"] is True
        for profile in ("RP-4G", "RP-8G"):
            block = evidence["profiles"][profile]
            assert block["enforced"] is True
            assert block["declared_memory_max_bytes"] in (4 * 2**30, 8 * 2**30)
            for peak in block["peaks"].values():
                assert peak["peak_bytes"] > 0
                assert peak["peak_bytes"] <= block["declared_memory_max_bytes"]
            if "peak_within_limit" in block:
                assert block["peak_within_limit"] is True


def test_release_evidence_covers_builds_locks_and_parity(release):
    assert release["required_check"] == "T-RELEASE"
    assert release["build"]["BUILD-GO-EDIT"]["p90_seconds"] <= 5
    assert release["build"]["BUILD-RUST-OP"]["p90_seconds"] <= 60
    assert release["build"]["BUILD-RELEASE"]["semantic_parity"] is True
    assert release["build"]["BUILD-RELEASE"]["binary_sha256"]
    assert release["parameter_only_change"]["native_jit_compilations"] == 0
    assert release["parameter_only_change"]["dag_hash_changed"] is True
    assert release["parameter_only_change"]["binary_unchanged"] is True
    assert release["correctness"]["all_passed"] is True
    locks = release["locks"]
    assert len(locks["nautilus_revision"]) == 40
    assert locks["pin_verification_status"] == "PASS"
    assert all(locks["pin_verification_checks"].values())


def test_acceptance_reports_missing_items_and_no_full_claim():
    acceptance = json.loads((EVIDENCE / "T25_ACCEPTANCE.json").read_text())
    assert acceptance["status"] == "PARTIAL/EVIDENCE_PENDING"
    assert acceptance["full_acceptance_claimed"] is False
    missing = " ".join(acceptance["missing_items"])
    assert "B4 100M" in missing
    assert "B2" in missing and "B3" in missing
    assert (EVIDENCE / "B4_100M_BLOCKED.json").exists()


def test_failed_attempts_are_retained_not_hidden(b0, b1, b5):
    for profile in ("RP-4G", "RP-8G"):
        native = b0["profiles"][profile]["native"]
        assert native["failed_attempts"] == []
        for evidence in (b1, b5):
            report = evidence["profiles"][profile]["report"]
            assert "failed_attempts" in report
    # A benchmark with any failed attempt is not allowed to pass.
    assert all(
        not b0["profiles"][profile]["native"]["failed_attempts"] for profile in ("RP-4G", "RP-8G")
    )
