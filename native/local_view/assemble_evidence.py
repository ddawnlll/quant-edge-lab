#!/usr/bin/env python3
"""Assemble the F22 two-world LocalView evidence.

This script never executes the native engine. It consumes the raw runner
outputs, recomputes the independent exact accounting with the C-ACCOUNT oracle,
checks the F22 invariants and the zero-delay control against the native runner,
and writes one self-bound evidence document.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from qel.accounting.core import dec, reconstruct

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "evidence" / "labd"

RUNS = {
    "control": EVIDENCE / "LOCAL_VIEW_CONTROL_ZERO.json",
    "main": EVIDENCE / "LOCAL_VIEW_F22_RUN.json",
    "large_response": EVIDENCE / "LOCAL_VIEW_F22_LARGE_RESPONSE_RUN.json",
    "decision_in_gap": EVIDENCE / "LOCAL_VIEW_F22_DECISION_IN_GAP_RUN.json",
}
NATIVE_CONTROL = EVIDENCE / "LOCAL_VIEW_NATIVE_CONTROL.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def accounting(tape: list[dict], native: dict) -> dict:
    out = reconstruct(tape)
    cash_diff = dec(out["total_cash"]) - dec(native["total"])
    equity_diff = dec(out["equity"]) - dec(native["equity"])
    position_diff = dec(out["position_qty"]) - dec(native["position_qty"])
    return {
        "reconstructed_cash": out["total_cash"],
        "reconstructed_equity": out["equity"],
        "reconstructed_position": out["position_qty"],
        "funding_cash": out["funding_cash"],
        "fee_total": out["fee_total"],
        "unrealized": out["unrealized"],
        "unexplained_residual": out["unexplained_residual"],
        "cash_diff": str(cash_diff),
        "equity_diff": str(equity_diff),
        "position_diff": str(position_diff),
        "match": cash_diff == 0
        and equity_diff == 0
        and position_diff == 0
        and out["unexplained_residual"] == "0",
    }


def local_accounting(tape: list[dict], local: dict) -> dict:
    out = reconstruct(tape)
    cash_diff = dec(out["total_cash"]) - dec(local["cash"])
    equity_diff = dec(out["equity"]) - dec(local["equity"])
    position_diff = dec(out["position_qty"]) - dec(local["position_qty"])
    return {
        "reconstructed_cash": out["total_cash"],
        "reconstructed_equity": out["equity"],
        "reconstructed_position": out["position_qty"],
        "funding_cash": out["funding_cash"],
        "fee_total": out["fee_total"],
        "unrealized": out["unrealized"],
        "unexplained_residual": out["unexplained_residual"],
        "cash_diff": str(cash_diff),
        "equity_diff": str(equity_diff),
        "position_diff": str(position_diff),
        "match": cash_diff == 0
        and equity_diff == 0
        and position_diff == 0
        and out["unexplained_residual"] == "0",
    }


def control_equivalence(control: dict, native: dict) -> dict:
    def events(tape: list[dict], kind: str) -> list[dict]:
        return [event for event in tape if event["type"] == kind]

    mismatches: list[str] = []
    fills_a = events(control["tape"], "FILL")
    fills_b = events(native["tape"], "FILL")
    if len(fills_a) != len(fills_b):
        mismatches.append(f"fill_count {len(fills_a)} != {len(fills_b)}")
    fields = ("ts_ns", "side", "qty", "price", "fee")
    for index, (left, right) in enumerate(zip(fills_a, fills_b)):
        for field in fields:
            if left.get(field) != right.get(field):
                mismatches.append(f"fill[{index}].{field} {left.get(field)!r} != {right.get(field)!r}")

    funding_a = events(control["tape"], "FUNDING")
    funding_b = events(native["tape"], "FUNDING")
    if len(funding_a) != len(funding_b):
        mismatches.append(f"funding_count {len(funding_a)} != {len(funding_b)}")
    funding_fields = ("ts_ns", "amount", "currency", "epoch", "settlement_id", "position_id", "account_id")
    for index, (left, right) in enumerate(zip(funding_a, funding_b)):
        for field in funding_fields:
            if left.get(field) != right.get(field):
                mismatches.append(
                    f"funding[{index}].{field} {left.get(field)!r} != {right.get(field)!r}"
                )

    mark_a = events(control["tape"], "MARK")
    mark_b = events(native["tape"], "MARK")
    if [m.get("price") for m in mark_a] != [m.get("price") for m in mark_b]:
        mismatches.append("mark price mismatch")

    account_fields = ("total", "free", "locked", "unrealized", "equity", "position_qty", "mark")
    account_diff = {
        field: [control["exchange"]["native_account"].get(field), native["native_account"].get(field)]
        for field in account_fields
        if control["exchange"]["native_account"].get(field) != native["native_account"].get(field)
    }
    if account_diff:
        mismatches.append(f"account mismatch {account_diff}")

    return {
        "native_run": str(NATIVE_CONTROL.relative_to(ROOT)),
        "native_status": native.get("status"),
        "control_status": control.get("status"),
        "fill_count": [len(fills_a), len(fills_b)],
        "funding_count": [len(funding_a), len(funding_b)],
        "field_mismatches": mismatches,
        "account_diff": account_diff,
        "semantically_equal": not mismatches,
        "expected_difference": (
            "Zero response delay is delivered on the next data callback rather than "
            "synchronously inside the exchange settlement callback. With "
            "feed_delay_ns=0 the policy cadence and tape are still field-identical; "
            "no order-timing difference was observed."
        ),
    }


def run_summary(run: dict) -> dict:
    return {
        "profile": run["profile"],
        "status": run["status"],
        "binding": run["binding"],
        "command": run["command"],
        "counts": run["counts"],
        "feed_delay": {
            key: value
            for key, value in run["f22"]["feed_delay_charged_once"].items()
            if "samples" not in key
        },
        "response_delay": run["f22"]["response_delay"],
        "exchange_before_delivery": run["f22"]["exchange_before_delivery"],
        "convergence": run["f22"]["convergence"],
        "cross_world_summary": run["f22"]["cross_world_summary"],
        "local_view": run["local_view"],
        "exchange": {
            "native_account": run["exchange"]["native_account"],
            "mark_ts_ns": run["exchange"]["mark_ts_ns"],
            "engine_iterations": run["exchange"]["engine_iterations"],
            "pnl_total": run["exchange"]["pnl_total"],
        },
        "resource": {
            key: run["resource"][key]
            for key in ("elapsed_ms", "peak_rss_kb", "events_path", "ts_last_ns")
        },
        "funding_deliveries": run["funding"],
        "artifact": None,
        "artifact_sha256": None,
        "artifact_bytes": None,
        "independent_accounting": None,
        "local_independent_accounting": None,
    }


def first_fill_with_gap(run: dict) -> dict | None:
    for fill in run["fills"]:
        if fill["decisions_in_gap"] > 0:
            return fill
    return None


def first_decision_with_in_flight(run: dict) -> dict | None:
    for decision in run["decisions"]:
        if decision["pre_existing_fills_in_flight"] > 0:
            return decision
    return None


def invariants(runs: dict[str, dict], control_check: dict) -> dict:
    main = runs["main"]
    large = runs["large_response"]
    gap = runs["decision_in_gap"]
    control = runs["control"]

    def all_fills_later(run: dict) -> dict:
        delay = int(run["command"]["response_delay_ns"])
        fills = run["fills"]
        later = [fill for fill in fills if int(fill["delivered_at"]) > int(fill["exchange_ts"])]
        before = [fill for fill in fills if int(fill["delivered_at"]) < int(fill["exchange_ts"])]
        return {
            "response_delay_ns": delay,
            "fills": len(fills),
            "strictly_later": len(later),
            "delivered_before_exchange_update": len(before),
            "pass": bool(fills) and len(before) == 0 and (delay == 0 or len(later) == len(fills)),
        }

    def feed_charged_once(run: dict) -> dict:
        feed = run["f22"]["feed_delay_charged_once"]
        delay = int(feed["feed_delay_ns"])
        min_total = int(feed["min_total_delay_ns"])
        return {
            "feed_delay_ns": delay,
            "items": feed["items"],
            "first_tick_violations": feed["first_tick_violations"],
            "delivered_before_due": feed["delivered_before_due"],
            "min_total_delay_ns": min_total,
            "max_total_delay_ns": int(feed["max_total_delay_ns"]),
            "max_lateness_ns": int(feed["max_lateness_ns"]),
            "pass": feed["first_tick_violations"] == 0
            and feed["delivered_before_due"] == 0
            and min_total >= delay,
        }

    def no_lookahead(run: dict) -> dict:
        response = run["f22"]["response_delay"]
        return {
            "fills": response["fills"],
            "fills_with_intervening_decisions": response["fills_with_intervening_decisions"],
            "decisions_with_unseen_fills": response["decisions_with_unseen_fills"],
            "policy_reads_before_delivery": response["policy_reads_before_delivery"],
            "max_in_flight_qty": response["max_in_flight_qty"],
            "sample_decision": first_decision_with_in_flight(run),
            "pass": response["policy_reads_before_delivery"] == 0,
        }

    def convergence(run: dict) -> dict:
        conv = run["f22"]["convergence"]
        position_diff = dec(conv["position_diff"])
        cash_diff = dec(conv["cash_diff"])
        equity_diff = dec(conv["equity_diff"])
        return {
            "local_position_qty": conv["local_position_qty"],
            "exchange_position_qty": conv["exchange_position_qty"],
            "position_diff": conv["position_diff"],
            "position_diff_numeric": str(position_diff),
            "local_cash": conv["local_cash"],
            "exchange_cash": conv["exchange_cash"],
            "cash_diff": conv["cash_diff"],
            "cash_diff_numeric": str(cash_diff),
            "local_equity": conv["local_equity"],
            "exchange_equity": conv["exchange_equity"],
            "equity_diff": conv["equity_diff"],
            "equity_diff_numeric": str(equity_diff),
            "pass": position_diff == 0 and cash_diff == 0 and equity_diff == 0,
        }

    def decision_gap_case(run: dict) -> dict:
        fills = run["fills"]
        with_decisions = [fill for fill in fills if fill["decisions_in_gap"] > 0]
        without_decisions = [fill for fill in fills if fill["decisions_in_gap"] == 0]
        return {
            "response_delay_ns": int(run["command"]["response_delay_ns"]),
            "fills": len(fills),
            "fills_with_decisions_in_gap": len(with_decisions),
            "fills_without_decisions_in_gap": len(without_decisions),
            "sample_fill": with_decisions[0] if with_decisions else None,
            "no_decision_in_any_gap": len(with_decisions) == 0,
            "pass": bool(fills)
            and all(
                dec(fill["local_position_before"])
                == dec(fill["exchange_position_before"])
                and dec(fill["local_position_after"])
                == dec(fill["exchange_position_after"])
                for fill in fills
            ),
        }

    return {
        "1_exchange_updates_before_response_delivery": {
            "main": all_fills_later(main),
            "large_response": all_fills_later(large),
            "decision_in_gap": all_fills_later(gap),
            "control_zero_delay": all_fills_later(control),
        },
        "2_no_policy_decision_uses_fill_before_delivery": {
            "main": no_lookahead(main),
            "large_response": no_lookahead(large),
            "decision_in_gap": no_lookahead(gap),
            "control_zero_delay": no_lookahead(control),
        },
        "3_feed_delay_charged_once": {
            "main": feed_charged_once(main),
            "large_response": feed_charged_once(large),
            "decision_in_gap": feed_charged_once(gap),
            "control_zero_delay": feed_charged_once(control),
        },
        "4_local_view_converges_to_exchange": {
            "main": convergence(main),
            "large_response": convergence(large),
            "decision_in_gap": convergence(gap),
            "control_zero_delay": convergence(control),
        },
        "5_zero_delay_control_equals_native_runner": control_check,
        "large_response_gap": {
            "large_response_30s": decision_gap_case(large),
            "decision_in_gap_90s": decision_gap_case(gap),
        },
    }


def invariant_pass(inv: dict) -> dict:
    groups = {
        "1_exchange_updates_before_response_delivery": all(
            item["pass"] for item in inv["1_exchange_updates_before_response_delivery"].values()
        ),
        "2_no_policy_decision_uses_fill_before_delivery": all(
            item["pass"]
            for item in inv["2_no_policy_decision_uses_fill_before_delivery"].values()
        ),
        "3_feed_delay_charged_once": all(
            item["pass"] for item in inv["3_feed_delay_charged_once"].values()
        ),
        "4_local_view_converges_to_exchange": all(
            item["pass"] for item in inv["4_local_view_converges_to_exchange"].values()
        ),
        "5_zero_delay_control_equals_native_runner": inv[
            "5_zero_delay_control_equals_native_runner"
        ]["semantically_equal"],
        "large_response_gap": all(
            item["pass"] for item in inv["large_response_gap"].values()
        ),
    }
    return {"groups": groups, "all_pass": all(groups.values())}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out",
        default=str(EVIDENCE / "LOCAL_VIEW_F22_2026-11-10.json"),
    )
    parser.add_argument(
        "--text-out",
        default=str(EVIDENCE / "LOCAL_VIEW_F22_2026-11-10.txt"),
    )
    args = parser.parse_args()

    raw = {name: load(path) for name, path in RUNS.items()}
    native = load(NATIVE_CONTROL)
    control_check = control_equivalence(raw["control"], native)

    summaries = {}
    for name, run in raw.items():
        summary = run_summary(run)
        path = RUNS[name]
        summary["artifact"] = str(path.relative_to(ROOT))
        summary["artifact_sha256"] = sha256_file(path)
        summary["artifact_bytes"] = path.stat().st_size
        summary["independent_accounting"] = accounting(run["tape"], run["exchange"]["native_account"])
        summary["local_independent_accounting"] = local_accounting(
            run["local_tape"], run["local_view"]
        )
        summaries[name] = summary

    all_invariants = invariants(raw, control_check)
    invariant_checks = invariant_pass(all_invariants)
    main_run = raw["main"]
    accounting_ok = all(
        summary["independent_accounting"]["match"]
        and summary["local_independent_accounting"]["match"]
        for summary in summaries.values()
    )
    run_status_ok = all(summary["status"] == "PASS" for summary in summaries.values())
    status_basis = {
        "runs_pass": run_status_ok,
        "f22_invariants_pass": invariant_checks["all_pass"],
        "exchange_accounting_pass": all(
            summary["independent_accounting"]["match"] for summary in summaries.values()
        ),
        "local_accounting_pass": all(
            summary["local_independent_accounting"]["match"]
            for summary in summaries.values()
        ),
        "control_equals_native_runner": control_check["semantically_equal"],
    }
    overall_pass = all(status_basis.values())
    status = "PASS_SCOPED_F22_PROFILE" if overall_pass else "FAIL"

    document = {
        "schema": "qel.local_view.f22.evidence.v1",
        "id": "LOCAL_VIEW_F22_2026-11-10",
        "date": "2026-11-10",
        "issue": [4, 11],
        "task": "TASK-11/TASK-18 F22 two-world delayed delivery (adapter-managed LocalView)",
        "status": status,
        "status_basis": status_basis,
        "claim_boundary": (
            "Adapter-managed LocalView inside a Nautilus strategy wrapper. Exchange world X is "
            "the pinned Nautilus BacktestEngine; strategy world Y is the qel-policy strategy, "
            "which reads only delivered LocalView fields. No fork, no monkey patch, no private "
            "API and no cache access from the policy path. This certifies F22 for the tested "
            "BTCUSDT L1 quote/trade archive profile only; it does not certify L2 queue parity, "
            "full order-ack/cancel latency, or backend-native LocalView."
        ),
        "binding": main_run["binding"],
        "reproduction": {
            "worktree": "/home/quant/quant/qel-lv",
            "base_sha": "ce91981055125328dae4b59add8e5432892c4f20",
            "build": "cd native/local_view && CARGO_TARGET_DIR=/home/quant/quant/.build-cache/qel-native cargo build --release -j 2",
            "test": "PYTHONPATH=python QEL_ROOT=$PWD /home/quant/quant/quant-edge-lab/.venv/bin/python -m pytest -q tests/test_local_view.py",
            "main": "qel-local-view --events /home/quant/quant/quant-edge-lab/data/native/DS-ARCHIVE-BTCUSDT-2026-11-07/events.jsonl --strategy policy/examples/taker_1m.json --feed-delay-ns 50000000 --response-delay-ns 25000000 --profile f22_main_feed50ms_response25ms --out evidence/labd/LOCAL_VIEW_F22_RUN.json",
            "large_response": "qel-local-view ... --feed-delay-ns 50000000 --response-delay-ns 30000000000 --profile f22_large_response_30s --out evidence/labd/LOCAL_VIEW_F22_LARGE_RESPONSE_RUN.json",
            "decision_in_gap": "qel-local-view ... --feed-delay-ns 50000000 --response-delay-ns 90000000000 --profile f22_decision_in_gap_90s --out evidence/labd/LOCAL_VIEW_F22_DECISION_IN_GAP_RUN.json",
            "control": "qel-local-view ... --feed-delay-ns 0 --response-delay-ns 0 --profile control_zero_delay --out evidence/labd/LOCAL_VIEW_CONTROL_ZERO.json",
            "native_control": "qel-native-runner --events ... --strategy policy/examples/taker_1m.json --out evidence/labd/LOCAL_VIEW_NATIVE_CONTROL.json",
            "independent_accounting": "PYTHONPATH=python QEL_ROOT=$PWD .venv/bin/python -m qel.cli accounting --tape <extracted tape>",
            "spec_manifest": "(cd specification && sha256sum -c MANIFEST.sha256)",
        },
        "runs": summaries,
        "f22_invariants": all_invariants,
        "f22_invariant_checks": invariant_checks,
        "cross_world_summary": {
            **main_run["f22"]["cross_world_summary"],
            "fill_side_breakdown": {
                "buy": sum(1 for fill in main_run["fills"] if fill["side"] == "BUY"),
                "sell": sum(1 for fill in main_run["fills"] if fill["side"] == "SELL"),
            },
            "time_to_delivery_ns": main_run["f22"]["response_delay"]["time_to_delivery_ns"],
            "fills_with_decisions_in_gap": main_run["f22"]["response_delay"][
                "fills_with_intervening_decisions"
            ],
            "policy_reads_before_delivery": main_run["f22"]["response_delay"][
                "policy_reads_before_delivery"
            ],
        },
        "independent_accounting": {
            name: summary["independent_accounting"] for name, summary in summaries.items()
        },
        "local_independent_accounting": {
            name: summary["local_independent_accounting"] for name, summary in summaries.items()
        },
        "resource": {
            "peak_rss_kb_max": max(s["resource"]["peak_rss_kb"] for s in summaries.values()),
            "elapsed_ms": {name: s["resource"]["elapsed_ms"] for name, s in summaries.items()},
            "slot": "exclusive Rust build/run; peak RSS < 2 GiB",
        },
        "recertification_impact": {
            "TASK-08": "The two-world callback surface is reachable through public Strategy/DataActor callbacks; LocalView is adapter-managed, not a backend-native immutable view.",
            "TASK-11": "F22 is now demonstrated for the taker L1 archive profile; the existing TASK-11 taker/funding certificate is unchanged.",
            "TASK-18": "native/local_view implements the delayed-delivery slice. Queue cross-check remains outside this evidence.",
            "CAP-LOCAL-VIEW": "RECERTIFIED only for the proven F22 profile. Not a full LocalView capability claim.",
        },
        "non_claims": [
            "No fork or monkey patch was used; LocalView is implemented in the strategy wrapper.",
            "Order accepted/rejected/canceled responses are not delayed in this slice; fills and funding account changes are delayed.",
            "The pinned engine has no generic deliver-at timer; delivery occurs on the first later subscribed data callback.",
            "L2 queue parity, queue cross-check, full latency calibration, multi-venue and mark-based liquidation are not claimed.",
            "The zero-delay control is semantically equal on the economic tape/account; synchronous delivery inside the exchange callback is not claimed.",
        ],
        "artifacts": {
            name: {
                "path": summary["artifact"],
                "sha256": summary["artifact_sha256"],
                "bytes": summary["artifact_bytes"],
            }
            for name, summary in summaries.items()
        }
        | {
            "native_control": {
                "path": str(NATIVE_CONTROL.relative_to(ROOT)),
                "sha256": sha256_file(NATIVE_CONTROL),
                "bytes": NATIVE_CONTROL.stat().st_size,
            }
        },
    }

    out = Path(args.out)
    out.write_text(json.dumps(document, indent=2, sort_keys=False) + "\n")
    print(f"wrote {out} ({out.stat().st_size} bytes)")

    lines = [
        "F22 two-world LocalView evidence",
        "================================",
        f"status: {document['status']}",
        f"source sha256: {document['binding']['source']['sha256']}",
        f"binary sha256: {document['binding']['binary']['sha256']}",
        f"corpus sha256: {document['binding']['corpus']['sha256']}",
        f"nautilus: {document['binding']['nautilus']['tag']} {document['binding']['nautilus']['commit']}",
        "",
    ]
    for name, summary in summaries.items():
        acc = summary["independent_accounting"]
        local_acc = summary["local_independent_accounting"]
        convergence_pass = document["f22_invariants"][
            "4_local_view_converges_to_exchange"
        ][
            "control_zero_delay" if name == "control" else name
        ]["pass"]
        lines += [
            f"[{name}] {summary['profile']} status={summary['status']}",
            f"  feed_delay_ns={summary['command']['feed_delay_ns']} response_delay_ns={summary['command']['response_delay_ns']}",
            f"  events={summary['counts']['events_parsed']} orders={summary['counts']['orders_submitted']} fills={summary['counts']['fills']} positions={summary['counts']['positions']}",
            f"  exchange residual={acc['unexplained_residual']} cash_diff={acc['cash_diff']} equity_diff={acc['equity_diff']} position_diff={acc['position_diff']}",
            f"  local residual={local_acc['unexplained_residual']} cash_diff={local_acc['cash_diff']} equity_diff={local_acc['equity_diff']} position_diff={local_acc['position_diff']}",
            f"  policy_reads_before_delivery={summary['response_delay']['policy_reads_before_delivery']} first_tick_violations={summary['feed_delay']['first_tick_violations']}",
            f"  convergence_pass={convergence_pass}",
            f"  peak_rss_kb={summary['resource']['peak_rss_kb']} elapsed_ms={summary['resource']['elapsed_ms']}",
            "",
        ]
    lines += [
        f"status basis: {json.dumps(document['status_basis'], sort_keys=True)}",
        f"f22 invariant groups: {json.dumps(document['f22_invariant_checks']['groups'], sort_keys=True)}",
        f"zero-delay control equal to native runner: {control_check['semantically_equal']}",
        f"control mismatches: {control_check['field_mismatches']}",
        f"large 30s fills with decisions in gap: {all_invariants['large_response_gap']['large_response_30s']['fills_with_decisions_in_gap']}",
        f"90s fills with decisions in gap: {all_invariants['large_response_gap']['decision_in_gap_90s']['fills_with_decisions_in_gap']}",
        f"90s policy_reads_before_delivery: {all_invariants['2_no_policy_decision_uses_fill_before_delivery']['decision_in_gap']['policy_reads_before_delivery']}",
    ]
    text_out = Path(args.text_out)
    text_out.write_text("\n".join(lines) + "\n")
    print(f"wrote {text_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
