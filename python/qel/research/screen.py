"""Preregistration, matched-control declarations and development screening."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ..accounting.core import dec, fmt, reconstruct
from ..contract.canonical import hash_jcs
from ..stats.core import classify, family_decision
from .protocol import (
    DAY_NS,
    build_calendar,
    preregistration_manifest,
    split_manifest as protocol_split_manifest,
)


def split_manifest(
    dates: list[str] | dict,
    symbol: str = "BTC",
    *,
    horizon_ns: int = DAY_NS,
    embargo_ns: int = 0,
    latency_ns: int = 0,
) -> dict:
    """Canonical TASK-12 calendar split (month-based, sealed reserve).

    ``dates`` may be a list of complete UTC days or a full calendar payload.
    The legacy day-count helper in ``stats.core`` is no longer the research
    split entry point.
    """
    payload = dates if isinstance(dates, dict) else {"symbol": symbol, "dates": dates}
    calendar = build_calendar(payload)
    return protocol_split_manifest(
        calendar, horizon_ns=horizon_ns, embargo_ns=embargo_ns, latency_ns=latency_ns
    )


def protocol_manifest(dates: list[str] | dict, symbol: str = "BTC", **kwargs) -> dict:
    """Freeze the preregistration manifest for a supplied daily calendar."""
    payload = dates if isinstance(dates, dict) else {"symbol": symbol, "dates": dates}
    calendar = build_calendar(payload)
    split = protocol_split_manifest(calendar, **kwargs)
    return preregistration_manifest(calendar, split)


def control_spec(family_id: str, seed: int, variants: int = 12) -> dict:
    return {
        "schema_version": "1.1.0",
        "family_id": family_id,
        "seed": seed,
        "draw_mapping": "HMAC_SHA256_COUNTER_V1",
        "seed_protocol_note": (
            "matched controls use the HMAC-SHA256 counter mapping in research/control.py; "
            "NumPy PCG64 is never the control engine"
        ),
        "matched_on": ["calendar", "trade_count", "holding_time"],
        "holding_time_note": "holding time is reported, not selected; use a holding-time target before claiming that match",
        "controls": [
            {"control_id": f"CTRL-{i}", "seed": hash_jcs({"seed": seed, "i": i})}
            for i in range(2)
        ],
        "variants": variants,
        "family_size": variants,
        "control_hash": hash_jcs({"seed": seed, "variants": variants}),
    }


def variant_grid(base: dict, count: int = 12) -> list[dict]:
    variants = []
    configs = [
        (3, 8, "0.75", "0.5"),
        (4, 10, "1", "0.5"),
        (5, 12, "1", "0.5"),
        (6, 16, "1.25", "0.5"),
        (8, 24, "1.5", "0.5"),
        (10, 32, "1.5", "0.5"),
        (5, 20, "1", "0.25"),
        (5, 20, "1", "0.75"),
        (7, 25, "1.25", "0.25"),
        (7, 25, "1.25", "0.75"),
        (12, 48, "2", "0.5"),
        (4, 12, "0.75", "0.5"),
    ]
    for i in range(min(count, len(configs))):
        ema, std, threshold, exit_level = configs[i]
        strategy = json.loads(json.dumps(base))
        strategy["strategy_id"] = f"STRAT-VAR-{i:02d}"
        for node in strategy["feature_dag"]:
            if node["node_id"] == "N-EMA":
                node["params"]["window"] = ema
            elif node["node_id"] == "N-STD":
                node["params"]["window"] = std
            elif node["node_id"] == "N-K":
                node["params"]["value"] = threshold
            elif node["node_id"] == "N-B":
                node["params"]["value"] = exit_level
        variants.append(strategy)
    return variants


def tape_net_pnl(tape: list[dict], capital: str = "1000") -> dict:
    filtered = [e for e in tape if e["type"] in ("SET_WALLET", "FILL", "MARK", "FUNDING", "END_FOLD")]
    # Ensure the wallet is explicit even if the producer omitted it.
    if not any(e["type"] == "SET_WALLET" for e in filtered):
        filtered.insert(0, {"type": "SET_WALLET", "cash": capital})
    out = reconstruct(filtered)
    net = dec(out["price_realized"]) - dec(out["fee_total"]) + dec(out["funding_cash"])
    return {
        "net_pnl": fmt(net),
        "realized": out["price_realized"],
        "fees": out["fee_total"],
        "funding": out["funding_cash"],
        "residual": out["unexplained_residual"],
        "fills": sum(1 for e in filtered if e["type"] == "FILL"),
    }


def run_variants(binary: str, events: str, base: dict, out_dir: str, count: int = 12) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    results = []
    for strategy in variant_grid(base, count):
        sid = strategy["strategy_id"]
        spath = out / f"{sid}.json"
        spath.write_text(json.dumps(strategy, indent=2))
        report = out / f"{sid}-report.json"
        proc = subprocess.run(
            [binary, "--events", events, "--strategy", str(spath), "--out", str(report), "--notional", "100"],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0 or not report.exists():
            results.append({"strategy_id": sid, "status": "BLOCKED", "error": proc.stderr[-500:]})
            continue
        data = json.loads(report.read_text())
        pnl = tape_net_pnl(data["tape"])
        if dec(pnl["residual"]) != 0:
            results.append(
                {
                    "strategy_id": sid,
                    "status": "BLOCKED",
                    "error": "ACCOUNTING_RESIDUAL",
                    "residual": pnl["residual"],
                }
            )
            continue
        results.append(
            {
                "strategy_id": sid,
                "status": "PASS",
                "orders": data.get("total_orders"),
                "positions": data.get("total_positions"),
                **pnl,
            }
        )
    nets = [dec(r["net_pnl"]) for r in results if r["status"] == "PASS"]
    verdict = "INSUFFICIENT_EVIDENCE"
    if nets:
        mean = sum(nets) / len(nets)
        # A development screen never promotes; only a frozen cohort can.
        verdict = classify(0.5, 0.1, float(mean) - 1.0, float(mean) + 1.0, 0.0001)
    return {
        "schema_version": "1.1.0",
        "status": (
            "PASS"
            if results and all(r["status"] == "PASS" for r in results)
            else ("PARTIAL" if results else "BLOCKED")
        ),
        "verdict": verdict,
        "variants": results,
        "variant_count": len(results),
        "family_note": "development screen only; no scientific promotion",
    }


def family_decision_report(
    candidates: dict,
    control: dict,
    capital: str,
    economic_floor: str,
    control_floor: str,
    **kwargs,
) -> dict:
    """Research family report backed by the frozen paired decision procedure.

    The report records both endpoints, the shared bootstrap/alpha inputs and the
    Holm-adjusted family table. A development-screen report never promotes: a
    statistical PROMOTE is surfaced as ``statistical_verdict`` while the report
    verdict stays INSUFFICIENT_EVIDENCE until a sealed cohort decides.
    """
    report = family_decision(candidates, control, capital, economic_floor, control_floor, **kwargs)
    statistical = report["verdict"]
    report["statistical_verdict"] = statistical
    report["promotion_boundary"] = "RESEARCH_SCREEN_NEVER_PROMOTES"
    if statistical == "PROMOTE":
        report["verdict"] = "INSUFFICIENT_EVIDENCE"
        report["boundary_reason"] = "RESEARCH_SCREEN_NEVER_PROMOTES"
    report["family_note"] = (
        "research family report: both endpoints, shared calendar and Holm inputs recorded; "
        "only a sealed cohort can promote"
    )
    return report
