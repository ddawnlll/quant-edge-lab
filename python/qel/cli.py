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
from .prepare.native_input import to_native_input
from .prepare.normalize import normalize_capture
from .research.screen import run_variants


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

    p = sub.add_parser("native-input", help="convert normalized Parquet to native JSONL")
    p.add_argument("--parquet", required=True)
    p.add_argument("--out", required=True)

    p = sub.add_parser("accounting", help="reconstruct an economic tape independently")
    p.add_argument("--tape", required=True)

    p = sub.add_parser("screen", help="run the frozen development variant family")
    p.add_argument("--binary", required=True)
    p.add_argument("--events", required=True)
    p.add_argument("--strategy", required=True)
    p.add_argument("--out", required=True)

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
        payload = normalize_capture(args.events, args.manifest, args.out, args.dataset_id)
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
    if args.command == "screen":
        base = json.loads(Path(args.strategy).read_text())
        payload = run_variants(args.binary, args.events, base, args.out, 12)
        _write(None, payload)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
