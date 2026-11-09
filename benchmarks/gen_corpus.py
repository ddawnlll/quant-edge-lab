#!/usr/bin/env python3
"""Freeze the TASK-25 benchmark corpora.

Every corpus is generated deterministically from a frozen generator version.
The manifest records the SHA-256 of each file plus the parameters that produced
it, so a timed run can be tied to exactly one corpus. `--check` re-generates in
memory and compares against the frozen manifest without rewriting files.

Corpora
-------
B0  policy-b0-256.json   256-node typed feature DAG, OPERATOR_SET_1_1.
B1  b1_bars.csv          1,000,000 integer sawtooth bars, 60 s interval.
    b1_workload.json     8 variants / 8 common operators / thresholds.
B5  b5_events.jsonl      100,000 deterministic ledger decision events.
    b5_manifest.json     1,000 artifact pointer definitions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys

GENERATOR_VERSION = "qel-bench-corpus-1.0.0"

ROOT = pathlib.Path(__file__).resolve().parent
CORPUS = ROOT / "corpus"

# OPERATOR_SET_1_1 identifiers used by the B0 DAG. Every node is taken from
# this frozen list; the B0 driver rejects anything else.
OPERATOR_SET_1_1 = [
    "OP-INPUT", "OP-CONST", "OP-ADD", "OP-SUB", "OP-MUL", "OP-DIV",
    "OP-LOG", "OP-EXP", "OP-ABS", "OP-LT", "OP-LE", "OP-GT", "OP-GE",
    "OP-AND", "OP-OR", "OP-NOT", "OP-LAG", "OP-SMA", "OP-SUM", "OP-MIN",
    "OP-MAX", "OP-EMA", "OP-STD", "OP-ER", "OP-PIVOT", "OP-QUANTILE",
]

STATELESS_F64 = ["OP-ADD", "OP-SUB", "OP-MUL", "OP-ABS"]
STATEFUL_F64 = ["OP-SMA", "OP-EMA", "OP-STD", "OP-ER", "OP-LAG"]

B1_ROWS = 1_000_000
B1_THRESHOLDS = ["1", "1.25", "1.5", "1.75", "2", "2.25", "2.5", "2.75"]
B1_BAR_INTERVAL_NS = 60_000_000_000
B1_COMMON_OPERATORS = [
    "OP-INPUT", "OP-LOG", "OP-EMA", "OP-SUB", "OP-STD", "OP-LAG", "OP-DIV", "OP-ER",
]

B5_EVENTS = 100_000
B5_ARTIFACTS = 1_000


def _node(node_id: str, op: str, inputs: list[str], output: str, params: dict | None = None) -> dict:
    return {
        "node_id": node_id,
        "operator_id": op,
        "inputs": inputs,
        "params": params or {},
        "output_type": output,
        "fit_scope": "NONE",
        "null_policy": "PROPAGATE_ABSTAIN",
        "clock": "BAR_CLOSE_DELIVERY",
    }


def build_b0_policy() -> dict:
    """256 nodes: input, log, 250-node chain, 4 boolean condition nodes."""
    dag: list[dict] = [_node("N-INPUT", "OP-INPUT", [], "F64", {"field": "MID_CLOSE"})]
    dag.append(_node("N-LOG", "OP-LOG", ["N-INPUT"], "F64"))
    # 250 chained nodes, 25 of them stateful (limit is 32).
    previous = "N-LOG"
    for i in range(250):
        idx = len(dag)
        stateful = i % 10 == 0
        if stateful:
            op = STATEFUL_F64[(i // 10) % len(STATEFUL_F64)]
            params = {"periods": 5} if op == "OP-LAG" else {"window": 5}
            if op == "OP-STD":
                params["ddof"] = 1
            dag.append(_node(f"N-{idx:03d}", op, [previous], "F64", params))
        else:
            op = STATELESS_F64[i % len(STATELESS_F64)]
            inputs = [previous] if op == "OP-ABS" else [previous, "N-LOG"]
            dag.append(_node(f"N-{idx:03d}", op, inputs, "F64"))
        previous = f"N-{idx:03d}"
    dag.append(_node("N-LONG", "OP-GT", [previous, "N-LOG"], "BOOL"))
    dag.append(_node("N-SHORT", "OP-LT", [previous, "N-LOG"], "BOOL"))
    dag.append(_node("N-LONG-EXIT", "OP-LE", [previous, "N-LOG"], "BOOL"))
    dag.append(_node("N-SHORT-EXIT", "OP-GE", [previous, "N-LOG"], "BOOL"))
    assert len(dag) == 256, len(dag)
    used = {n["operator_id"] for n in dag}
    assert used <= set(OPERATOR_SET_1_1), used - set(OPERATOR_SET_1_1)
    return {
        "schema_version": "1.1.0",
        "strategy_id": "STRAT-BENCH-B0-256",
        "hypothesis_id": "HYP-BENCH-B0",
        "operator_set_version": "1.1.0",
        "decision_clock": {
            "kind": "BAR_CLOSE",
            "interval_ns": "60000000000",
            "bar_source": "QUOTE_MID",
            "stale_after_ns": "120000000000",
            "on_order_response": False,
        },
        "feature_dag": dag,
        "entry_policy": {
            "kind": "THRESHOLD_TAKER",
            "long_condition_node": "N-LONG",
            "short_condition_node": "N-SHORT",
            "both_true": "ABSTAIN",
            "max_positions_per_instrument": 1,
            "limit_offset_ticks": 0,
            "time_in_force": "IOC",
            "post_only": False,
            "cooldown_ns": "60000000000",
            "limit_ttl_ns": "60000000000",
            "refresh_on_new_decision": False,
        },
        "exit_policy": {
            "kind": "BRACKET_AND_SIGNAL",
            "long_exit_node": "N-LONG-EXIT",
            "short_exit_node": "N-SHORT-EXIT",
            "stop_sigma_node": "N-002",
            "stop_multiplier": "2",
            "sigma_freeze": "ENTRY",
            "stop_type": "STOP_MARKET",
            "take_profit": "SIGNAL_ONLY",
            "target_r": None,
            "partial_fraction": "0",
            "after_partial": "KEEP_STOP",
            "contingency": "CLIENT_MANAGED",
            "max_holding_ns": "3600000000000",
            "reduce_only": True,
            "stop_widening": False,
            "target_r_secondary": None,
            "stop_limit_offset_ticks": 0,
        },
        "sizing_policy": {
            "kind": "FIXED_RISK",
            "risk_fraction": "0.001",
            "notional_i": None,
            "lot_rounding": "FLOOR",
            "max_leverage": "1",
            "cost_buffer_rate": "0.001",
            "on_minimum_violation": "REJECT",
            "max_open_stop_risk_fraction": "0.02",
        },
        "risk_profile_hash": "00" * 32,
        "venue_profile_hash": "00" * 32,
        "latency_profile_hash": "00" * 32,
        "required_capabilities": ["CAP-TYPED-POLICY"],
        "max_stateful_operators": 32,
        "free_search_parameters": ["PARAM-K"],
        "max_active_orders": 1,
        "max_holding_ns": "3600000000000",
        "initial_state": "FLAT",
        "terminal_valuation": "LAST_VALID_MARK_MINUS_CLOSE_COST",
        "source": "backtest",
    }


def b1_rows():
    """Yield (i, open, close, high, low, volume) for the integer sawtooth."""
    previous_close = 100000
    for i in range(B1_ROWS):
        close = 100000 + (i % 1000)
        open_ = previous_close
        high = max(open_, close) + 2
        low = min(open_, close) - 2
        volume = 1000 + (i % 97)
        yield i, open_, close, high, low, volume
        previous_close = close


def render_b1_csv() -> bytes:
    lines = ["i,open,close,high,low,volume"]
    for i, open_, close, high, low, volume in b1_rows():
        lines.append(f"{i},{open_},{close},{high},{low},{volume}")
    return ("\n".join(lines) + "\n").encode()


def b1_workload() -> dict:
    return {
        "generator_version": GENERATOR_VERSION,
        "kind": "B1_BATCH_SCREEN",
        "rows": B1_ROWS,
        "variants": len(B1_THRESHOLDS),
        "variant_thresholds": B1_THRESHOLDS,
        "common_operators": B1_COMMON_OPERATORS,
        "bar_interval_ns": str(B1_BAR_INTERVAL_NS),
        "generation": (
            "integer sawtooth: close_i=100000+(i%1000); open_i=previous_close "
            "(open_0=100000); high_i=max(open,close)+2; low_i=min(open,close)-2; "
            "volume_i=1000+i%97"
        ),
        "feature_plan": {
            "v1": "OP-INPUT close",
            "v2": "OP-LOG(v1)",
            "v3": "OP-EMA(v2,48)",
            "v4": "OP-SUB(v2,v3)",
            "v5": "OP-STD(v4,96,ddof=1)",
            "v6": "OP-LAG(v4,1)",
            "v7": "OP-DIV(v4,v5)  score",
            "v8": "OP-ER(v2,10)",
        },
        "variant_rule": "long if score>t and er>0; short if score<-t and er>0; else flat",
        "include": ["READ", "FEATURES", "VARIANTS", "OUTPUT_HASH_FSYNC"],
    }


def b5_events() -> list[dict]:
    events = []
    for i in range(B5_EVENTS):
        kind = i % 10
        if kind in (0, 1, 2, 3):
            event_type = "DecisionRecorded"
            payload = {
                "decision_id": f"DEC-{i:06d}",
                "variant": i % 8,
                "bar_index": i,
                "action": ["ENTER_LONG", "ENTER_SHORT", "EXIT", "HOLD"][i % 4],
                "score_ppm": (i * 7919) % 2000001 - 1000000,
            }
            scope = "SCIENTIFIC"
        elif kind in (4, 5):
            event_type = "PassRecorded"
            payload = {
                "pass_id": f"PASS-{i:06d}",
                "gate": f"G{i % 5}",
                "verdict": "PASS",
                "evidence_hash": hashlib.sha256(f"pass-{i}".encode()).hexdigest(),
                "complete": True,
            }
            scope = "SCIENTIFIC"
        elif kind in (6, 7):
            event_type = "ArtifactPointer"
            payload = {
                "artifact_index": i % B5_ARTIFACTS,
                "pointer_sha256": hashlib.sha256(f"artifact-{i % B5_ARTIFACTS}".encode()).hexdigest(),
            }
            scope = "OPERATIONS"
        else:
            event_type = "JobStateChanged"
            payload = {"job_id": f"JOB-{i:06d}", "from": "QUEUED", "to": "RUNNING"}
            scope = "OPERATIONS"
        events.append(
            {
                "event_type": event_type,
                "effective_at_ns": str(1_700_000_000_000_000_000 + i * 1_000_000),
                "payload": payload,
                "record_scope": scope,
            }
        )
    return events


def b5_manifest() -> dict:
    pointers = []
    for i in range(B5_ARTIFACTS):
        pointers.append(
            {
                "artifact_id": f"ART-B5-{i:04d}",
                "path": f"b5/artifact_{i:04d}.bin",
                "sha256": hashlib.sha256(f"artifact-{i}".encode()).hexdigest(),
                "size_bytes": 64,
            }
        )
    return {
        "generator_version": GENERATOR_VERSION,
        "kind": "B5_LEDGER_RESTORE",
        "ledger_events": B5_EVENTS,
        "artifact_pointers": B5_ARTIFACTS,
        "crash_matrix": "F17",
        "consistency_check": "SQLITE_BACKUP_AND_FULL_HASH_CHAIN",
        "artifact_pointers_list": pointers,
    }


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build() -> dict[str, bytes]:
    policy = json.dumps(build_b0_policy(), indent=2, sort_keys=True) + "\n"
    workload = json.dumps(b1_workload(), indent=2, sort_keys=True) + "\n"
    events = "".join(json.dumps(e, sort_keys=True) + "\n" for e in b5_events())
    manifest = json.dumps(b5_manifest(), indent=2, sort_keys=True) + "\n"
    return {
        "b0_policy_256.json": policy.encode(),
        "b1_bars.csv": render_b1_csv(),
        "b1_workload.json": workload.encode(),
        "b5_events.jsonl": events.encode(),
        "b5_artifacts.json": manifest.encode(),
    }


def write_manifest(files: dict[str, bytes]) -> dict:
    return {
        "kind": "TASK25_FROZEN_CORPUS_MANIFEST",
        "generator_version": GENERATOR_VERSION,
        "frozen_before_timed_run": True,
        "files": {
            name: {"sha256": sha256(data), "bytes": len(data)}
            for name, data in sorted(files.items())
        },
        "counts": {
            "b0_dag_nodes": len(build_b0_policy()["feature_dag"]),
            "b1_rows": B1_ROWS,
            "b1_variants": len(B1_THRESHOLDS),
            "b5_ledger_events": B5_EVENTS,
            "b5_artifact_pointers": B5_ARTIFACTS,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="verify the frozen manifest")
    parser.add_argument("--json", action="store_true", help="print the manifest only")
    args = parser.parse_args()
    files = build()
    manifest = write_manifest(files)
    if args.check:
        frozen = json.loads((CORPUS / "MANIFEST.json").read_text())
        problems = []
        for name, entry in frozen["files"].items():
            actual = sha256(files[name])
            if actual != entry["sha256"]:
                problems.append(f"{name}: frozen={entry['sha256']} actual={actual}")
        status = "PASS" if not problems and frozen["files"] == manifest["files"] else "FAIL"
        print(json.dumps({"status": status, "problems": problems, "manifest": manifest}, indent=2))
        return 0 if status == "PASS" else 1
    if args.json:
        print(json.dumps(manifest, indent=2))
        return 0
    CORPUS.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (CORPUS / name).write_bytes(data)
    (CORPUS / "MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
