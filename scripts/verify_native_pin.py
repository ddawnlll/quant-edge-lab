#!/usr/bin/env python3
"""Verify the native source/build pin against local and upstream artifacts."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "locks/SOURCE_PIN.json"
RUNTIME = ROOT / "locks/RUNTIME_LOCK.json"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    source = json.loads(SOURCE.read_text())
    runtime = json.loads(RUNTIME.read_text())
    clone = Path("/home/quant/quant/src-clones/nautilus")
    head = subprocess.run(["git", "-C", str(clone), "rev-parse", "HEAD"], text=True, capture_output=True, check=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "-C", str(clone), "status", "--porcelain"], text=True, capture_output=True, check=True).stdout.strip())
    tag = subprocess.run(
        ["git", "-C", str(clone), "tag", "--points-at", "HEAD"], text=True, capture_output=True, check=True
    ).stdout.split()
    remote = subprocess.run(
        ["git", "ls-remote", source["source_repository"], f"refs/tags/{source['tag']}", f"refs/tags/{source['tag']}^{{}}"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    remote_commits = {line.split()[1]: line.split()[0] for line in remote.splitlines()}
    lockfile = ROOT / "native/spike/Cargo.lock"
    spike_bin = Path("/home/quant/quant/.build-cache/qel-native/release/qel-native-spike")
    spike_main = ROOT / "native/spike/src/main.rs"
    spike_report = ROOT / "evidence/native/SPIKE_REPORT.json"
    fixture_corpus = ROOT / "specification/fixtures/conformance.json"
    checks = {
        "head_matches_pin": head == source["commit"],
        "worktree_clean": not dirty,
        "tag_points_at_head": source["tag"] in tag,
        "remote_tag_dereferences_to_pin": remote_commits.get(f"refs/tags/{source['tag']}^{{}}") == source["commit"],
        "cargo_lock_hash_matches": sha(lockfile) == source["dependency_lock_hash"],
        "spike_source_hash_matches": sha(spike_main) == runtime["lock_artifact_hashes"][1],
        "spike_report_hash_matches": sha(spike_report) == runtime["native_spike_report_hash"],
        "binary_hash_matches": sha(spike_bin) == source["binary_sha256"],
        "binary_size_matches": spike_bin.stat().st_size == source["binary_bytes"],
        "fixture_corpus_hash_matches": sha(fixture_corpus) == source["fixture_corpus_sha256"],
        "runtime_lock_unresolved": runtime["status"] == "UNRESOLVED",
        "verified_profile_present": "TAKER_L1_FUNDING_STREAMING" in runtime["verified_profile_ids"],
    }
    out = {
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "head": head,
        "tag": source["tag"],
        "remote_tag_commit": remote_commits.get(f"refs/tags/{source['tag']}^{{}}"),
        "source_pin_sha256": sha(SOURCE),
        "runtime_lock_sha256": sha(RUNTIME),
    }
    print(json.dumps(out, indent=2))
    return 0 if out["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
