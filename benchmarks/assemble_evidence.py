#!/usr/bin/env python3
"""Assemble TASK-25 B0/B1/B5 evidence from raw per-profile runs.

Raw reports live in evidence/labd/raw/ and are produced by run_all_bench.sh,
which enforces the declared memory profile with a cgroup v2 scope. This script
never re-times anything: it only merges the raw measurements with the frozen
corpus manifest, the environment lock and the acceptance decision.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import platform
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
RAW = ROOT / "evidence/labd/raw"
EVIDENCE = ROOT / "evidence/labd"
CORPUS = ROOT / "benchmarks/corpus"
PROFILES = {"RP-4G": "4G", "RP-8G": "8G"}
LIMITS = {"RP-4G": 4096 * 1024 * 1024, "RP-8G": 8192 * 1024 * 1024}


def sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def load(path: pathlib.Path):
    if not path.exists():
        return None
    return json.loads(path.read_text())


def tool_version(cmd: list[str]) -> str | None:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if out.returncode != 0:
            return None
        return out.stdout.strip().splitlines()[0]
    except Exception:
        return None


def cpu_model() -> str:
    for line in pathlib.Path("/proc/cpuinfo").read_text().splitlines():
        if line.startswith("model name"):
            return line.split(":", 1)[1].strip()
    return "unknown"


def environment() -> dict:
    meminfo = pathlib.Path("/proc/meminfo").read_text().splitlines()
    mem_total = next((int(l.split()[1]) for l in meminfo if l.startswith("MemTotal")), 0)
    return {
        "host": platform.node(),
        "platform": platform.platform(),
        "kernel": platform.release(),
        "cpu_model": cpu_model(),
        "cpu_count": subprocess.run(["nproc"], capture_output=True, text=True).stdout.strip(),
        "memory_total_kib": mem_total,
        "python": platform.python_version(),
        "go": tool_version(["go", "version"]),
        "rustc": tool_version(["rustc", "--version"]),
        "cargo": tool_version(["cargo", "--version"]),
        "systemd": tool_version(["systemctl", "--version"]),
        "cgroup_version": "v2",
        "profile_mechanism": "transient systemd user scope with MemoryMax and MemorySwapMax=0",
        "swap_disabled_in_scope": True,
    }


def profile_block(profile: str, peak_files: dict[str, str]) -> dict:
    block = {
        "id": profile,
        "declared_memory_max_bytes": LIMITS[profile],
        "enforcement": "CGROUP_V2_MEMORY_MAX",
        "enforced": False,
        "memory_swap_max_bytes": 0,
        "peaks": {},
    }
    for name, peak in peak_files.items():
        path = RAW / peak
        if path.exists():
            peak_bytes = int(path.read_text().strip() or 0)
            block["peaks"][name] = {"peak_bytes": peak_bytes, "peak_mib": round(peak_bytes / 2**20, 2)}
            if peak_bytes > 0:
                block["enforced"] = True
    if block["peaks"]:
        block["peak_within_limit"] = all(
            p["peak_bytes"] <= LIMITS[profile] for p in block["peaks"].values()
        )
    return block


def merge_profile(profile: str, raw_name: str, peak_name: str) -> dict | None:
    raw = load(RAW / raw_name)
    if raw is None:
        return None
    block = profile_block(profile, {"run": peak_name})
    if raw.get("failed_attempts") is None:
        raw["failed_attempts"] = []
    block["report"] = raw
    return block


def b0_evidence(manifest: dict) -> dict:
    profiles = {}
    for profile in PROFILES:
        native = merge_profile(profile, f"B0_NATIVE_{profile}.json", f"B0_NATIVE_{profile}.peak")
        schema = merge_profile(profile, f"B0_SCHEMA_{profile}.json", f"B0_SCHEMA_{profile}.peak")
        if native is None or schema is None:
            profiles[profile] = {"status": "NOT_RUN"}
            continue
        profiles[profile] = {
            "enforcement": "CGROUP_V2_MEMORY_MAX",
            "enforced": native["enforced"] and schema["enforced"],
            "declared_memory_max_bytes": LIMITS[profile],
            "peaks": {**{f"native_{k}": v for k, v in native["peaks"].items()},
                      **{f"schema_only_{k}": v for k, v in schema["peaks"].items()}},
            "native": native["report"],
            "schema_only": schema["report"],
        }
    per_profile_targets = {}
    for profile, block in profiles.items():
        if "native" not in block:
            per_profile_targets[profile] = {"status": "NOT_RUN"}
            continue
        native = block["native"]
        schema = block["schema_only"]
        p90 = native["metric"]["value"]
        per_profile_targets[profile] = {
            "p90_ms_native": p90,
            "p90_ms_schema_only": schema["metric"]["value"],
            "p90_target_ms": 100,
            "p90_passed": p90 <= 100,
            "native_jit_compilations": native["native_jit_compilations"],
            "native_jit_compilations_target": 0,
            "native_jit_compilations_passed": native["native_jit_compilations"] == 0,
            "dag_nodes": native["dag_nodes_measured"],
            "dag_nodes_target": 256,
            "dag_nodes_passed": native["dag_nodes_measured"] == 256,
            "timed_repeats": native["timed"]["repeats"],
            "warmups": native["warmups"],
            "failed_attempts": native["failed_attempts"],
            "verdict": native["verdict"],
        }
    all_targets = all(
        v.get("p90_passed") and v.get("native_jit_compilations_passed") and v.get("dag_nodes_passed")
        and not v.get("failed_attempts") and v.get("verdict") == "PASS"
        for v in per_profile_targets.values()
    )
    enforced = all(b.get("enforced") for b in profiles.values())
    return {
        "kind": "TASK25_B0_TYPED_SPECIFICATION",
        "task": "TASK-25",
        "required_check": "B0",
        "title": "Typed specification: 256-node OPERATOR_SET_1_1 DAG",
        "corpus": {
            "path": "benchmarks/corpus/b0_policy_256.json",
            "sha256": manifest["files"]["b0_policy_256.json"]["sha256"],
            "manifest_sha256": sha256_file(CORPUS / "MANIFEST.json"),
            "frozen_before_timed_run": True,
        },
        "include": ["PARSE", "SCHEMA", "TYPECHECK", "HASH"],
        "schema_only_vs_native_separate": True,
        "cold_warm_boundary": "binary built before timing; 2 warmups discarded; 12 timed repeats",
        "profiles": profiles,
        "per_profile_targets": per_profile_targets,
        "targets_met": bool(all_targets and enforced),
        "profiles_enforced": enforced,
        "correctness": {
            "required": ["T-POLICY", "F21"],
            "evidence": "tests/test_benchmarks.py::test_b0_correctness_reference + T_RELEASE_BUILD.json#conformance",
        },
        "status": "PASS" if all_targets and enforced else "EVIDENCE_PENDING",
    }


def b1_evidence(manifest: dict) -> dict:
    profiles = {}
    for profile in PROFILES:
        block = merge_profile(profile, f"B1_{profile}.json", f"B1_{profile}.peak")
        profiles[profile] = block or {"status": "NOT_RUN"}
    per_profile_targets = {}
    for profile, block in profiles.items():
        if "report" not in block:
            per_profile_targets[profile] = {"status": "NOT_RUN"}
            continue
        report = block["report"]
        p90 = report["metric"]["value"]
        per_profile_targets[profile] = {
            "warm_p90_seconds": p90,
            "target_seconds": 10,
            "passed": p90 <= 10,
            "timed_repeats": report["timed"]["repeats"],
            "warmups": report["warmups"],
            "failed_attempts": report["failed_attempts"],
            "reference_verification": report["reference_verification"],
            "verdict": report["verdict"],
        }
    all_targets = all(
        v.get("passed") and v.get("verdict") == "PASS" and not v.get("failed_attempts")
        and v.get("reference_verification", {}).get("status") == "PASS"
        for v in per_profile_targets.values()
    )
    enforced = all(b.get("enforced") for b in profiles.values())
    return {
        "kind": "TASK25_B1_BATCH_SCREEN",
        "task": "TASK-25",
        "required_check": "B1",
        "title": "Batch screen: 1,000,000 rows x 8 variants",
        "corpus": {
            "path": "benchmarks/corpus/b1_bars.csv",
            "sha256": manifest["files"]["b1_bars.csv"]["sha256"],
            "workload_sha256": manifest["files"]["b1_workload.json"]["sha256"],
            "manifest_sha256": sha256_file(CORPUS / "MANIFEST.json"),
            "frozen_before_timed_run": True,
            "rows": manifest["counts"]["b1_rows"],
            "variants": manifest["counts"]["b1_variants"],
        },
        "include": ["READ", "FEATURES", "VARIANTS", "OUTPUT_HASH_FSYNC"],
        "output_hash_fsync_inside_timed_region": True,
        "cold_warm_boundary": "corpus frozen and page cache warm; 2 warmups discarded; 12 timed repeats",
        "profiles": profiles,
        "per_profile_targets": per_profile_targets,
        "targets_met": bool(all_targets and enforced),
        "profiles_enforced": enforced,
        "correctness": {
            "required": ["T-POLICY", "F18"],
            "evidence": "tests/test_benchmarks.py::test_b1_correctness_reference + T_RELEASE_BUILD.json#conformance",
        },
        "status": "PASS" if all_targets and enforced else "EVIDENCE_PENDING",
    }


def b5_evidence(manifest: dict) -> dict:
    profiles = {}
    for profile in PROFILES:
        block = merge_profile(profile, f"B5_{profile}.json", f"B5_{profile}.peak")
        profiles[profile] = block or {"status": "NOT_RUN"}
    per_profile_targets = {}
    for profile, block in profiles.items():
        if "report" not in block:
            per_profile_targets[profile] = {"status": "NOT_RUN"}
            continue
        report = block["report"]
        # The standalone F17 matrix is the authoritative crash result. It runs
        # after the disk benchmark so the SIGKILL trials cannot perturb the read
        # timings; the in-band matrix from the main run is kept for comparison.
        crash = load(RAW / f"B5_CRASH_{profile}.json")
        if crash and crash.get("report"):
            report["crash_matrix"]["in_band_report"] = report["crash_matrix"].get("report")
            report["crash_matrix"]["report"] = crash["report"]
            report["crash_matrix"]["authoritative_source"] = f"evidence/labd/raw/B5_CRASH_{profile}.json"
            report["crash_matrix"]["separate_from_timed_reads"] = True
            lost = crash["report"]["lost_committed_decisions"]
            torn = crash["report"]["torn_pass"]
            report["targets"]["lost_committed_decisions"] = {
                "value": lost, "target": 0, "operator": "EQ", "passed": lost == 0}
            report["targets"]["torn_pass"] = {
                "value": torn, "target": 0, "operator": "EQ", "passed": torn == 0}
        per_profile_targets[profile] = {
            **report["targets"],
            "verdict": report["verdict"],
            "ledger_events": report["ledger_events_total"],
            "artifact_pointers": report["artifact_pointers_sealed"],
            "crash_matrix_status": report["crash_matrix"]["report"].get("status"),
        }
    def targets_ok(v: dict) -> bool:
        return (
            v.get("verdict") == "PASS"
            and v.get("crash_matrix_status") == "PASS"
            and v.get("ledger_events") == 101000
            and v.get("artifact_pointers") == 1000
            and all(v[k]["passed"] for k in
                    ("lost_committed_decisions", "torn_pass", "read_transaction_ms", "metadata_restore_minutes"))
        )
    all_targets = all(targets_ok(v) for v in per_profile_targets.values())
    enforced = all(b.get("enforced") for b in profiles.values())
    return {
        "kind": "TASK25_B5_LEDGER_RESTORE",
        "task": "TASK-25",
        "required_check": "B5",
        "title": "Ledger and restore: 100,000 events, 1,000 artifact pointers, F17",
        "corpus": {
            "path": "benchmarks/corpus/b5_events.jsonl",
            "sha256": manifest["files"]["b5_events.jsonl"]["sha256"],
            "artifact_declaration_sha256": manifest["files"]["b5_artifacts.json"]["sha256"],
            "manifest_sha256": sha256_file(CORPUS / "MANIFEST.json"),
            "frozen_before_timed_run": True,
            "ledger_events": manifest["counts"]["b5_ledger_events"],
            "artifact_pointers": manifest["counts"]["b5_artifact_pointers"],
        },
        "profiles": profiles,
        "per_profile_targets": per_profile_targets,
        "targets_met": bool(all_targets and enforced),
        "profiles_enforced": enforced,
        "correctness": {
            "required": ["F17", "F25", "F26"],
            "evidence": "F17 crash matrix in each profile report; labd/ledger and labd/artifacts test suites",
        },
        "status": "PASS" if all_targets and enforced else "EVIDENCE_PENDING",
    }


def main() -> int:
    manifest = json.loads((CORPUS / "MANIFEST.json").read_text())
    env = environment()
    b0 = b0_evidence(manifest)
    b1 = b1_evidence(manifest)
    b5 = b5_evidence(manifest)
    for item, name in ((b0, "B0_TYPED_SPECIFICATION.json"), (b1, "B1_BATCH_SCREEN.json"), (b5, "B5_LEDGER_RESTORE.json")):
        item["environment"] = env
        (EVIDENCE / name).write_text(json.dumps(item, indent=2, sort_keys=True) + "\n")
        print(f"wrote {name}: {item['status']}")

    # Whole-task acceptance is deliberately narrower than the individual checks.
    missing = []
    if not b0["targets_met"]:
        missing.append("B0 target/profile not met")
    if not b1["targets_met"]:
        missing.append("B1 target/profile not met")
    if not b5["targets_met"]:
        missing.append("B5 target/profile not met")
    b4_blocked = load(EVIDENCE / "B4_100M_BLOCKED.json")
    if b4_blocked:
        missing.append("B4 100M events: BLOCKED_NOT_RUN (see evidence/labd/B4_100M_BLOCKED.json)")
    for check in ("B2", "B3", "B4"):
        missing.append(
            f"{check}: prior TASK-19 evidence exists but was not re-run under the enforced RP-4G/RP-8G profiles"
        )
    acceptance = {
        "kind": "TASK25_ACCEPTANCE",
        "task": "TASK-25",
        "required_checks": ["B0", "B1", "B2", "B3", "B4", "B5", "T-RELEASE"],
        "check_status": {
            "B0": b0["status"],
            "B1": b1["status"],
            "B2": "EVIDENCE_PENDING_PROFILE_ENFORCEMENT",
            "B3": "EVIDENCE_PENDING_PROFILE_ENFORCEMENT",
            "B4": "EVIDENCE_PENDING_100M_AND_PROFILE_ENFORCEMENT",
            "B5": b5["status"],
            "T-RELEASE": "SEE_T_RELEASE_BUILD",
        },
        "status": "PARTIAL/EVIDENCE_PENDING",
        "missing_items": missing,
        "full_acceptance_claimed": False,
    }
    (EVIDENCE / "T25_ACCEPTANCE.json").write_text(json.dumps(acceptance, indent=2, sort_keys=True) + "\n")
    print("wrote T25_ACCEPTANCE.json: PARTIAL/EVIDENCE_PENDING")
    return 0


if __name__ == "__main__":
    sys.exit(main())
