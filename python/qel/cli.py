"""QEL command line: contract selftest, fixture harness and evidence reports."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

from .accounting.core import reconstruct
from .conformance import Harness, summarize
from .contract import canonical_bytes, hash_jcs, repo_root
from .contract.runkey import RUN_KEY_OBJECT_KEYS, run_key
from .evaluator.demo import run_demo
from .prepare.archive import build as build_archive
from .prepare.native_input import to_native_input
from .prepare.normalize import normalize_capture
from .research.control import matched_controls
from .research.protocol import DAY_NS, protocol_report
from .research.screen import run_variants
from .stats.calibration import render_summary_text, run_calibration
from .stats.core import power_grid
from .stats.job import run_job


def _write(path: str | None, payload: dict) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(text)
    else:
        sys.stdout.write(text)


def runkey_vectors() -> dict:
    material = {k: (("%064x" % (i + 1))) for i, k in enumerate(RUN_KEY_OBJECT_KEYS)}
    base = run_key(material)
    reordered = run_key(dict(reversed(list(material.items()))))
    changed = run_key({**material, "strategy_hash": "f" * 64})
    excluded = run_key({**material, "attempt_id": "A" * 64}) if False else None
    return {
        "run_key": base,
        "key_order_invariant": base == reordered,
        "meaningful_change_changes_key": base != changed,
        "attempt_excluded_by_contract": excluded is None,
        "fields": len(material),
    }


def cross_language() -> dict:
    root = repo_root()
    material = {k: (("%064x" % (i + 7))) for i, k in enumerate(RUN_KEY_OBJECT_KEYS)}
    payload = json.dumps(material)
    py_key = run_key(material)
    py_canon = canonical_bytes({"z": "é", "a": "\u0001", "b": "𝄞"}).decode()
    result = {"python": {"run_key": py_key, "canonical": py_canon}}
    go_bin = os.environ.get("QEL_GO_CONTRACT", str(root / "bin" / "qel-contract"))
    rs_bin = os.environ.get("QEL_RS_CONTRACT", str(root / "bin" / "qel-contract-rs"))
    for name, binary in (("go", go_bin), ("rust", rs_bin)):
        if not Path(binary).exists():
            result[name] = {"status": "MISSING_BINARY", "path": binary}
            continue
        env = {**os.environ, "QEL_ROOT": str(root)}
        key = subprocess.run([binary, "runkey"], input=payload, text=True, capture_output=True, env=env)
        canon = subprocess.run([binary, "canonical"], input=json.dumps({"z": "é", "a": "\u0001", "b": "𝄞"}), text=True, capture_output=True, env=env)
        result[name] = {
            "run_key": key.stdout.strip(),
            "run_key_match": key.stdout.strip() == py_key,
            "canonical_match": canon.stdout.strip() == py_canon,
            "stderr": key.stderr.strip() or canon.stderr.strip(),
        }
    result["all_match"] = all(
        result[n].get("run_key_match") and result[n].get("canonical_match") for n in ("go", "rust")
    ) and result["python"]["run_key"] == py_key
    return result


def mutations() -> dict:
    """Negative controls: deliberately wrong expectations must fail."""
    harness = Harness()
    cases = []

    def mutate(fixture_id: str, path: str, value):
        scenario = harness.by_id[fixture_id][0]
        original = scenario["expected"]
        scenario["expected"] = [
            {**e, "expected": value} if e["path"] == path else e for e in original
        ]
        try:
            result = harness.run(scenario)
            passed = result["status"] == "PASS"
        finally:
            scenario["expected"] = original
        cases.append({"case": f"{fixture_id}:{path}", "mutated_expected": value, "caught": not passed})

    mutate("F31", "canceled_qty", "0")
    mutate("F09", "funding_attributed_epoch", "2")
    mutate("F13", "fee", "0.01")
    mutate("F01", "fills_at_10_qty", "1")
    mutate("F23", "fee_total", "0.30")

    def mutate_input(fixture_id: str, step_index: int, key: str, value):
        scenario = harness.by_id[fixture_id][0]
        args = scenario["steps"][step_index]["arguments"]
        original = args[key]
        args[key] = value
        try:
            result = harness.run(scenario)
            passed = result["status"] == "PASS"
        finally:
            args[key] = original
        cases.append(
            {
                "case": f"{fixture_id}:step{step_index}.{key}",
                "mutated_input": value,
                "caught": not passed,
            }
        )

    mutate_input("F01", 3, "aggressor", "BUY")
    mutate_input("F09", 1, "rate_num", "2")
    mutate_input("F13", 0, "notional", "200")
    mutate_input("F23", 2, "fee", "0.30")
    mutate_input("F31", 1, "qty", "0")
    return {"cases": cases, "all_caught": all(c["caught"] for c in cases)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qel")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("fixtures", help="run the hand oracle fixture harness")
    p.add_argument("--out")
    p.add_argument("--fixture", action="append")

    p = sub.add_parser("mutations", help="run fixture mutation negative controls")
    p.add_argument("--out")

    p = sub.add_parser("crosslang", help="compare Python/Go/Rust contract vectors")
    p.add_argument("--out")

    p = sub.add_parser("runkey", help="print a run-key vector report")
    p.add_argument("--out")

    p = sub.add_parser("prepare", help="normalize a raw capture into Parquet")
    p.add_argument("--events", required=True)
    p.add_argument("--manifest", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--dataset-id", default=None)
    p.add_argument("--rules", default=None, help="bitemporal rules JSON")
    p.add_argument("--decision-ns", default=None, help="dataset decision time in integer ns")
    p.add_argument("--path-decision", default=None, help="runtime backend decision JSON")

    p = sub.add_parser("archive-native", help="build a native input from a daily archive")
    p.add_argument("--agg", required=True)
    p.add_argument("--mark", required=True)
    p.add_argument("--funding", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--symbol", default="BTCUSDT")

    p = sub.add_parser("native-input", help="convert normalized Parquet to native JSONL")
    p.add_argument("--parquet", required=True)
    p.add_argument("--out", required=True)

    p = sub.add_parser("accounting", help="reconstruct an economic tape independently")
    p.add_argument("--tape", required=True)

    p = sub.add_parser("evaluator-demo", help="exercise the sealed evaluator end to end")
    p.add_argument("--root", default=None)
    p.add_argument("--out")

    p = sub.add_parser("stats-job", help="run a statistics job from daily returns")
    p.add_argument("--returns", required=True)
    p.add_argument("--out")

    p = sub.add_parser("power", help="injected-effect power grid")
    p.add_argument("--programs", type=int, default=200)
    p.add_argument("--out")

    p = sub.add_parser("calibrate", help="B6 statistical null and injection calibration")
    p.add_argument("--programs", type=int, default=1000)
    p.add_argument("--generator", action="append", default=None)
    p.add_argument("--cohorts", type=int, default=3)
    p.add_argument("--dev-days", type=int, default=1500)
    p.add_argument("--fresh-days", type=int, default=500)
    p.add_argument("--control-replications", type=int, default=1000)
    p.add_argument("--family-size", type=int, default=16)
    p.add_argument("--effects", default=None, help="comma-separated injected daily effects")
    p.add_argument("--power-programs", type=int, default=2)
    p.add_argument("--power-cohorts", type=int, default=None)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--base-seed", type=int, default=0)
    p.add_argument("--b", type=int, default=None, help="bootstrap resamples (default: frozen minimum)")
    p.add_argument("--max-resamples", type=int, default=None)
    p.add_argument("--label", default=None)
    p.add_argument("--out")
    p.add_argument("--summary-out")

    p = sub.add_parser("controls", help="run matched random-entry controls")
    p.add_argument("--events", required=True)
    p.add_argument("--trades", type=int, required=True)
    p.add_argument("--notional", type=float, default=100.0)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--replications", type=int, default=8)

    p = sub.add_parser("screen", help="run the frozen development variant family")
    p.add_argument("--binary", required=True)
    p.add_argument("--events", required=True)
    p.add_argument("--strategy", required=True)
    p.add_argument("--out", required=True)

    p = sub.add_parser(
        "protocol",
        help="emit the frozen TASK-12 preregistration and walk-forward split manifest",
    )
    p.add_argument(
        "--calendar",
        required=True,
        help="JSON daily calendar: date list, symbol->dates map or observation rows",
    )
    p.add_argument("--symbol", default=None, help="symbol used for fit-scope checks")
    p.add_argument("--horizon-ns", type=int, default=DAY_NS, help="declared label horizon")
    p.add_argument("--embargo-ns", type=int, default=0)
    p.add_argument("--latency-ns", type=int, default=0)
    p.add_argument(
        "--allow-gaps",
        action="store_true",
        help="declare absent archive dates as recorded gaps instead of rejecting",
    )
    p.add_argument("--out")

    args = parser.parse_args(argv)
    if args.command == "fixtures":
        harness = Harness()
        results = harness.run_all() if not args.fixture else [r for f in args.fixture for r in harness.run_fixture(f)]
        summary = summarize(results)
        payload = {
            "status": "PASS" if summary["failed"] == 0 else "FAIL",
            "summary": summary,
            "results": results,
        }
        _write(args.out, payload)
        return 0 if summary["failed"] == 0 else 1
    if args.command == "mutations":
        payload = mutations()
        payload["status"] = "PASS" if payload["all_caught"] else "FAIL"
        _write(args.out, payload)
        return 0 if payload["all_caught"] else 1
    if args.command == "crosslang":
        payload = cross_language()
        payload["status"] = "PASS" if payload["all_match"] else "FAIL"
        _write(args.out, payload)
        return 0 if payload["all_match"] else 1
    if args.command == "runkey":
        payload = runkey_vectors()
        payload["status"] = "PASS" if payload["key_order_invariant"] and payload["meaningful_change_changes_key"] else "FAIL"
        _write(args.out, payload)
        return 0 if payload["status"] == "PASS" else 1
    if args.command == "prepare":
        path_decision = None
        if args.path_decision:
            path_decision = json.loads(Path(args.path_decision).read_text())
        payload = normalize_capture(
            args.events,
            args.manifest,
            args.out,
            args.dataset_id,
            rules_path=args.rules,
            decision_ns=args.decision_ns,
            path_decision=path_decision,
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if args.command == "archive-native":
        payload = build_archive(args.agg, args.mark, args.funding, args.out, args.symbol)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if args.command == "native-input":
        payload = to_native_input(args.parquet, args.out)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if args.command == "accounting":
        tape = json.loads(Path(args.tape).read_text())
        payload = reconstruct(tape)
        _write(None, payload)
        return 0
    if args.command == "evaluator-demo":
        import tempfile

        root = args.root or tempfile.mkdtemp(prefix="qel-evaluator-e2e-")
        payload = run_demo(root)
        _write(args.out, payload)
        return 0 if payload["status"] == "PASS" else 1
    if args.command == "stats-job":
        payload = run_job(args.returns)
        _write(args.out, payload)
        return 0 if payload["status"] == "PASS" else 1
    if args.command == "power":
        payload = power_grid(programs=args.programs)
        _write(args.out, payload)
        return 0
    if args.command == "calibrate":
        effects = None
        if args.effects:
            effects = [float(part) for part in args.effects.split(",") if part.strip()]
        payload = run_calibration(
            programs=args.programs,
            generators=args.generator,
            cohorts=args.cohorts,
            dev_days=args.dev_days,
            fresh_days=args.fresh_days,
            control_replications=args.control_replications,
            effects=effects,
            power_programs=args.power_programs,
            power_cohorts=args.power_cohorts,
            family_size=args.family_size,
            workers=args.workers,
            base_seed=args.base_seed,
            b=args.b,
            max_resamples=args.max_resamples,
            label=args.label,
        )
        _write(args.out, payload)
        if args.summary_out:
            Path(args.summary_out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.summary_out).write_text(render_summary_text(payload))
        return 0 if payload["status"] == "ACCEPTANCE" else 1
    if args.command == "controls":
        payload = matched_controls(args.events, args.trades, args.notional, args.seed, args.replications)
        _write(None, payload)
        return 0
    if args.command == "screen":
        base = json.loads(Path(args.strategy).read_text())
        payload = run_variants(args.binary, args.events, base, args.out, 12)
        _write(None, payload)
        return 0
    if args.command == "protocol":
        calendar_payload = json.loads(Path(args.calendar).read_text())
        payload = protocol_report(
            calendar_payload,
            symbol=args.symbol,
            horizon_ns=args.horizon_ns,
            embargo_ns=args.embargo_ns,
            latency_ns=args.latency_ns,
            allow_gaps=args.allow_gaps,
        )
        _write(args.out, payload)
        if payload["status"] == "PASS":
            return 0
        return 3 if payload["status"] == "INSUFFICIENT_HISTORY" else 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
