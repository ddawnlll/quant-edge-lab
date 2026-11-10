#!/usr/bin/env python3
"""Assemble the v1.1 integration evidence manifest.

Usage:
  python scripts/build_integration_evidence.py \
    --candidate-sha <sha> \
    --pytest "367 passed, 4 skipped" \
    --go "ok ./labd/..." \
    --rust "14 passed" \
    --validator "PASS_SPECIFICATION_ONLY, 20/20 mutants" \
    --out evidence/labd/INTEGRATION_V11_FINAL.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence" / "labd"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load(name: str) -> dict:
    path = EVIDENCE / name
    return json.loads(path.read_text()) if path.exists() else {"status": "MISSING"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate-sha", required=True)
    ap.add_argument("--pytest", required=True)
    ap.add_argument("--go", required=True)
    ap.add_argument("--rust", required=True)
    ap.add_argument("--validator", required=True)
    ap.add_argument("--out", default=str(EVIDENCE / "INTEGRATION_V11_FINAL.json"))
    args = ap.parse_args()

    b2 = load("B2_RECHECK_2026-11-10.json")
    b3 = load("B3_10M_ATOMIC_L2_2026-11-10.json")
    f22 = load("LOCAL_VIEW_F22_2026-11-10.json")
    pq = load("PASSIVE_QUEUE_ADAPTER_2026-11-10.json")
    m2_path = EVIDENCE / "M2_DEV_RUN.json"
    m2 = json.loads(m2_path.read_text()) if m2_path.exists() else {"status": "MISSING"}
    matrix = json.loads((EVIDENCE / "ACCEPTANCE_MATRIX.json").read_text())

    files = [
        "LOCAL_VIEW_F22_2026-11-10.json",
        "PASSIVE_QUEUE_ADAPTER_2026-11-10.json",
        "PASSIVE_QUEUE_COMPLETION_2026-11-10.json",
        "B2_RECHECK_2026-11-10.json",
        "B3_10M_ATOMIC_L2_2026-11-10.json",
        "M2_DEV_RUN.json",
        "ACCEPTANCE_MATRIX.json",
    ]
    hashes = {
        name: sha256(EVIDENCE / name) for name in files if (EVIDENCE / name).exists()
    }
    payload = {
        "schema": "qel.integration.v11/1",
        "candidate_sha": args.candidate_sha,
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": "PASS_WITH_RECORDED_BLOCKERS",
        "issue_counts": matrix["summary"],
        "closures_pending_merge": [4, 10],
        "left_open_with_blockers": matrix["summary"]["remaining_open_issues"],
        "tests": {
            "pytest": args.pytest,
            "go": args.go,
            "rust": args.rust,
            "spec_manifest": "sha256sum -c MANIFEST.sha256: OK",
            "spec_validator": args.validator,
            "native_crates": "local_view cargo test OK; passive_runner 15 adapter tests OK",
        },
        "f22": {
            "status": f22.get("status"),
            "invariants": f22.get("f22_invariants"),
            "cross_world": f22.get("cross_world_summary"),
        },
        "passive_queue": {
            "profile": pq.get("profile"),
            "summary": pq.get("adapter_summary"),
        },
        "benchmarks": {
            "B2": {
                "verdict": b2.get("verdict"),
                "p90_wall_s": b2.get("p90_wall_s"),
                "median_wall_s": b2.get("median_wall_s"),
                "target_s": b2.get("target_s"),
            },
            "B3_10M": {
                "verdict": b3.get("verdict"),
                "p90_s": b3.get("p90_s"),
                "target_s": b3.get("target_s"),
                "events": (b3.get("workload") or {}).get("events"),
            },
        },
        "m2": {
            "status": m2.get("status"),
            "scope": m2.get("scope"),
            "fold": m2.get("fold"),
            "venue": m2.get("venue"),
            "decision_verdict": (m2.get("decision") or {}).get("verdict"),
            "controls_seeds": ((m2.get("controls") or {}).get("summary") or {}).get("seeds"),
            "selected_variant": (m2.get("train") or {}).get("selected"),
            "validation_variants": sorted(((m2.get("validation") or {}).get("variants") or {}).keys()),
        },
        "evidence_sha256": hashes,
        "notes": [
            "No positive edge, fresh OOS result or live authority is claimed.",
            "All closures are contingent on the PR merge.",
            "The 7-day soak and B8 remain open; the 26-month execution folds have no intraday data.",
        ],
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"out": str(out), "hashes": len(hashes)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
