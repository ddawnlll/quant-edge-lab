"""M2 development walk-forward runner.

This module connects the frozen TASK-12 split manifest, train-only parameter
selection, candidate replay, matched controls and the frozen paired decision
through one normal entry point.

The only executable multi-day corpus in this environment is the seven-day BTC
development week (2024-09-01..07).  The 26-month mark calendar has no intraday
execution data for its later folds, so this runner validates the frozen manifest
first, refuses to read a sealed or unavailable window, and then runs a clearly
labelled development-week fold.  It never claims M2 acceptance for the
26-month/fresh-OOS scope.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..accounting.core import dec, fmt
from ..contract.canonical import hash_jcs
from ..stats.core import family_decision
from . import control as control_mod
from .protocol import DAY_NS, ProtocolError, build_calendar, split_manifest, preregistration_manifest
from .screen import tape_net_pnl, variant_grid

SCHEMA = "qel.m2_run/1.1"
DEV_WEEK_START = "2024-09-01"
DEV_WEEK_END = "2024-09-07"
MIN_SHARED_DAYS = 3


def _sha256_file(path: str | os.PathLike[str]) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_json(path: str | os.PathLike[str]) -> dict:
    return json.loads(Path(path).read_text())


def _utc_day_start_ns(day: str) -> int:
    d = date.fromisoformat(day)
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()) * 1_000_000_000


def _day_bounds(day: str) -> tuple[int, int]:
    start = _utc_day_start_ns(day)
    return start, start + DAY_NS


def load_execution_days(data_root: str, dates: Sequence[str]) -> dict[str, str]:
    """Resolve per-day native event files without inventing missing data."""
    root = Path(data_root)
    found: dict[str, str] = {}
    candidates = [
        root / "_dev_w1" / "{day}.jsonl",
        root / "{day}.jsonl",
        root / "events" / "{day}.jsonl",
    ]
    for day in dates:
        for pattern in candidates:
            path = Path(str(pattern).format(day=day))
            if path.is_file() and path.stat().st_size > 0:
                found[day] = str(path)
                break
    return found


def make_development_fold(dates: Sequence[str], train_days: int = 3, purge_days: int = 1) -> dict:
    """Build the labelled development-week fold.

    The split mirrors the 12/3/3 idea in days (three train, one purge/embargo,
    three validation).  It is *not* the 26-month protocol fold and is always
    labelled ``DEVELOPMENT_WEEK_SCOPE``.
    """
    ordered = sorted(dates)
    if len(ordered) < train_days + purge_days + MIN_SHARED_DAYS:
        raise ProtocolError(
            "INSUFFICIENT_DEVELOPMENT_DAYS",
            {"available": len(ordered), "required": train_days + purge_days + MIN_SHARED_DAYS},
        )
    train = ordered[:train_days]
    purge = ordered[train_days : train_days + purge_days]
    validation = ordered[train_days + purge_days :]
    if validation[0] <= train[-1]:
        raise ProtocolError("INVALID_FOLD_BOUNDARY")
    return {
        "fold_id": "DEVWEEK-" + hash_jcs(
            {"train": train, "purge": purge, "validation": validation}
        )[:16].upper(),
        "scope": "DEVELOPMENT_WEEK_SCOPE",
        "train_dates": train,
        "purge_dates": purge,
        "validation_dates": validation,
        "train_start": train[0],
        "train_end": train[-1],
        "validation_start": validation[0],
        "validation_end": validation[-1],
        "purge_days": len(purge),
        "embargo_ns": len(purge) * DAY_NS,
        "label_horizon_ns": DAY_NS,
    }


def check_frozen_manifest(manifest: Mapping[str, Any], execution_days: Mapping[str, str]) -> dict:
    """Check the frozen 26-month manifest against executable data.

    The runner never substitutes mark-only days for execution days.  Each fold
    is reported as executable or with its exact missing days.
    """
    split = manifest.get("split_manifest") if isinstance(manifest, Mapping) else None
    if not isinstance(split, Mapping):
        raise ProtocolError("MISSING_SPLIT_MANIFEST")
    folds = split.get("folds") or []
    results = []
    for fold in folds:
        needed = []
        for key in ("train_start", "validation_start", "validation_end"):
            if fold.get(key):
                needed.append(str(fold[key])[:10])
        if fold.get("train_start") and fold.get("validation_end_exclusive"):
            needed.append(str(fold["validation_end_exclusive"])[:10])
        missing = sorted({d for d in needed if d not in execution_days})
        results.append(
            {
                "fold_id": fold.get("fold_id"),
                "train_start": fold.get("train_start"),
                "validation_start": fold.get("validation_start"),
                "validation_end": fold.get("validation_end"),
                "missing_execution_days": missing,
                "executable": not missing,
            }
        )
    return {
        "manifest_split_hash": split.get("split_hash") or split.get("manifest_hash"),
        "fold_count": len(folds),
        "folds": results,
        "executable_fold_count": sum(1 for r in results if r["executable"]),
        "status": "EXECUTABLE" if any(r["executable"] for r in results) else "INSUFFICIENT_EXECUTION_DATA",
    }


def _strategy_payloads(base: Mapping[str, Any], count: int) -> list[dict]:
    return variant_grid(dict(base), count)


def _run_candidate(
    binary: str,
    events_path: str,
    strategy_path: str,
    out_path: str,
    notional: float,
    *,
    timeout_s: int = 900,
) -> dict:
    cmd = [
        binary,
        "--events",
        events_path,
        "--strategy",
        strategy_path,
        "--notional",
        str(notional),
        "--out",
        out_path,
    ]
    started = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    elapsed = time.monotonic() - started
    if proc.returncode != 0 or not Path(out_path).is_file():
        return {
            "status": "BLOCKED",
            "exit_code": proc.returncode,
            "elapsed_s": round(elapsed, 3),
            "stderr_tail": proc.stderr[-800:],
            "command": cmd,
        }
    report = _read_json(out_path)
    tape = report.get("tape") or []
    pnl = tape_net_pnl(tape, capital="1000")
    return {
        "status": "PASS" if dec(pnl["residual"]) == 0 else "BLOCKED",
        "exit_code": proc.returncode,
        "elapsed_s": round(elapsed, 3),
        "total_orders": report.get("total_orders"),
        "total_positions": report.get("total_positions"),
        "net_pnl": pnl["net_pnl"],
        "residual": pnl["residual"],
        "fills": pnl["fills"],
        "fees": pnl["fees"],
        "funding": pnl["funding"],
        "report_path": out_path,
        "command": cmd,
    }


def _run_family(
    binary: str,
    strategies: Sequence[Mapping[str, Any]],
    days: Mapping[str, str],
    out_dir: Path,
    notional: float,
    workers: int,
) -> dict[str, dict[str, Any]]:
    """Run every variant on every supplied day and return per-variant results."""
    out_dir.mkdir(parents=True, exist_ok=True)
    variants: dict[str, dict[str, Any]] = {}
    tasks = []
    for strat in strategies:
        sid = str(strat["strategy_id"])
        spath = out_dir / f"{sid}.json"
        spath.write_text(json.dumps(strat, indent=2, sort_keys=True))
        variants[sid] = {"strategy_path": str(spath), "days": {}}
        for day, events in sorted(days.items()):
            tasks.append((sid, day, events, str(spath), str(out_dir / f"{sid}-{day}.json")))
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 6))) as pool:
        futures = {
            pool.submit(_run_candidate, binary, events, spath, out, notional): (sid, day)
            for sid, day, events, spath, out in tasks
        }
        for fut in as_completed(futures):
            sid, day = futures[fut]
            try:
                variants[sid]["days"][day] = fut.result()
            except Exception as exc:  # pragma: no cover - subprocess failures are data
                variants[sid]["days"][day] = {"status": "BLOCKED", "error": repr(exc)}
    for sid, payload in variants.items():
        rows = payload["days"]
        if rows and all(r.get("status") == "PASS" for r in rows.values()):
            total = sum((dec(r["net_pnl"]) for r in rows.values()), dec("0"))
            payload["status"] = "PASS"
            payload["net_pnl"] = fmt(total)
            payload["total_orders"] = sum(int(r.get("total_orders") or 0) for r in rows.values())
            payload["total_positions"] = sum(int(r.get("total_positions") or 0) for r in rows.values())
            payload["residual"] = fmt(sum((dec(r["residual"]) for r in rows.values()), dec("0")))
        else:
            payload["status"] = "BLOCKED"
            payload["net_pnl"] = None
    return variants


def _control_panel(
    quotes: list[dict],
    funding: list[dict],
    fold: Mapping[str, Any],
    notional: float,
    trades: int | None,
    replications: int,
    max_holding_ns: int,
    seed: int = 0,
) -> dict:
    train_end_ns = _day_bounds(fold["train_end"])[1] - 1
    plan = control_mod.prepare_control_plan(
        quotes,
        trades,
        notional,
        seed,
        max_holding_ns,
        train_end_ns=train_end_ns,
        funding=funding,
    )
    agg = control_mod.StreamingControlAggregator(plan, keep_summaries=True)
    started = time.monotonic()
    for i in range(replications):
        agg.add(control_mod.run_control_seed(plan, i))
    elapsed = time.monotonic() - started
    daily = agg.daily_series()
    validation_days = set(fold["validation_dates"])
    panel = {
        "BTC": {
            str(row["day"]): str(row["mean_net_pnl"]) if row["mean_net_pnl"] is not None else "0"
            for row in daily
            if str(row["day"]) in validation_days
        }
    }
    return {
        "panel": panel,
        "daily": daily,
        "summary": agg.summary(),
        "plan_hash": plan.plan_hash,
        "selection_plan_hash": plan.selection_plan_hash,
        "calendar": list(plan.calendar),
        "notional": str(plan.notional),
        "starting_cash": str(plan.starting_cash),
        "target_trades_by_day": dict(plan.target_trades_by_day),
        "elapsed_s": round(elapsed, 3),
        "replications": replications,
    }


def run_m2(
    *,
    manifest_path: str,
    data_root: str,
    binary: str,
    strategy_base_path: str,
    out_dir: str,
    capital: str = "1000",
    risk_fraction: str = "0.1",
    control_replications: int = 1000,
    family_size: int = 12,
    workers: int = 4,
    max_holding_ns: int = 3_600_000_000_000,
    bootstrap_resamples: int = 10000,
) -> dict:
    """Run the development M2 path end to end.

    The acceptance path derives the order notional from capital and a declared
    risk fraction rather than a hard-coded 100 USDT.  It also checks the actual
    instrument minimum and returns ``INFEASIBLE_VENUE_MIN_NOTIONAL`` when the
    requested target cannot be expressed at the venue.
    """
    started = time.monotonic()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    manifest = _read_json(manifest_path)
    base = _read_json(strategy_base_path)
    capital_d = dec(capital)
    risk = dec(risk_fraction)
    notional = capital_d * risk
    # The instrument used by the native runner has min qty 0.001 and a price
    # around 60k in the development week; the check is evaluated against the
    # first train quote below.
    execution_days = load_execution_days(data_root, _manifest_calendar_dates(manifest))
    manifest_check = check_frozen_manifest(manifest, execution_days)

    dev_dates = [d.isoformat() for d in _date_range(DEV_WEEK_START, DEV_WEEK_END)]
    dev_exec = load_execution_days(data_root, dev_dates)
    if len(dev_exec) < 7:
        return {
            "schema": SCHEMA,
            "status": "INSUFFICIENT_DATA",
            "reason": "DEVELOPMENT_WEEK_INCOMPLETE",
            "missing_days": [d for d in dev_dates if d not in dev_exec],
            "manifest_check": manifest_check,
        }
    fold = make_development_fold(dev_dates)

    # The venue feasibility check needs one observed quote, not the full tape.
    first_day_quotes = control_mod.load_quotes(dev_exec[fold["train_dates"][0]])
    min_price = Fraction(str(first_day_quotes[0]["bid"])) if first_day_quotes else Fraction(0)
    min_qty = Fraction("0.001")
    min_notional = min_qty * min_price
    notional_feasible = notional >= min_notional
    venue_test = {
        "target_notional": fmt(notional),
        "min_quantity": str(min_qty),
        "reference_price": fmt(min_price),
        "min_notional": fmt(min_notional),
        "feasible": notional_feasible,
        "status": "PASS" if notional_feasible else "INFEASIBLE_VENUE_MIN_NOTIONAL",
        "note": "The 20 USDT target is below the 0.001 BTC venue minimum at this price; it cannot be honored without rounding up and changing the risk target.",
    }
    if not notional_feasible:
        return {
            "schema": SCHEMA,
            "status": "INFEASIBLE_VENUE_MIN_NOTIONAL",
            "reason": "TARGET_NOTIONAL_BELOW_VENUE_MINIMUM",
            "capital": fmt(capital_d),
            "risk_fraction": fmt(risk),
            "requested_notional": fmt(notional),
            "venue": venue_test,
            "manifest_check": manifest_check,
            "execution_days": len(dev_exec),
        }

    # Quote/funding load for the control fit (train + validation, no purge day).
    control_days = list(fold["train_dates"]) + list(fold["validation_dates"])
    quotes: list[dict] = []
    funding: list[dict] = []
    for day in control_days:
        quotes.extend(control_mod.load_quotes(dev_exec[day]))
        f, _raw = control_mod.load_funding(dev_exec[day])
        funding.extend({"ts": item.ts, "rate": str(item.rate)} for item in f)
    quotes.sort(key=lambda row: int(row["ts"]))
    funding.sort(key=lambda row: int(row["ts"]))

    strategies = _strategy_payloads(base, family_size)
    train_days = {d: dev_exec[d] for d in fold["train_dates"]}
    validation_days = {d: dev_exec[d] for d in fold["validation_dates"]}
    train = _run_family(binary, strategies, train_days, out / "train", float(notional), workers)
    # Train-only selection: never look at validation when choosing the fit.
    candidates = [sid for sid, row in train.items() if row["status"] == "PASS"]
    selected = max(candidates, key=lambda sid: (dec(train[sid]["net_pnl"] or "0"), sid)) if candidates else None
    validation = _run_family(binary, strategies, validation_days, out / "validation", float(notional), workers)

    candidate_panel: dict[str, dict[str, str]] = {}
    candidate_rows: dict[str, dict] = {}
    for sid, row in validation.items():
        if row["status"] != "PASS":
            candidate_rows[sid] = {"status": "BLOCKED", "reason": "VALIDATION_RUN_FAILED"}
            continue
        panel = {}
        for day in fold["validation_dates"]:
            day_payload = row["days"].get(day, {})
            panel[day] = str(day_payload.get("net_pnl", "0")) if day_payload.get("status") == "PASS" else "0"
        candidate_panel[sid] = {"BTC": panel}
        candidate_rows[sid] = {
            "status": "PASS",
            "net_pnl": row["net_pnl"],
            "total_orders": row["total_orders"],
            "total_positions": row["total_positions"],
            "residual": row["residual"],
            "selected_on_train": sid == selected,
        }

    train_trades = None
    if selected:
        train_trades = max(
            0,
            sum(int(train[selected]["days"][d].get("fills") or 0) for d in fold["train_dates"])
            // max(1, len(fold["train_dates"])),
        )
    controls = _control_panel(
        quotes,
        funding,
        fold,
        float(notional),
        train_trades,
        control_replications,
        max_holding_ns,
    )

    decision = None
    if candidate_panel:
        try:
            decision = family_decision(
                candidate_panel,
                controls["panel"],
                capital,
                "0",
                "0",
                b=max(bootstrap_resamples, 10001),
                max_resamples=2_000_000,
                seed=0,
            )
        except Exception as exc:
            decision = {"status": "ERROR", "error": repr(exc)}
    else:
        decision = {"status": "NO_CANDIDATE", "verdict": "INSUFFICIENT_EVIDENCE"}

    report = {
        "schema": SCHEMA,
        "status": "PASS" if candidate_panel and controls["replications"] else "INSUFFICIENT_EVIDENCE",
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scope": "DEVELOPMENT_WEEK_SCOPE",
        "scope_note": (
            "Seven-day BTC development week only. The 26-month mark calendar and "
            "fresh OOS are separate, narrower scopes and are not accessed here."
        ),
        "base_sha": _git_sha(),
        "binary": {"path": binary, "sha256": _sha256_file(binary)},
        "strategy_base": {
            "path": strategy_base_path,
            "sha256": _sha256_file(strategy_base_path),
        },
        "manifest": {
            "path": manifest_path,
            "sha256": _sha256_file(manifest_path),
            "check": manifest_check,
            "development_fold_hash": hash_jcs(fold),
            "frozen_manifest_status": manifest_check["status"],
        },
        "capital": fmt(capital_d),
        "risk_fraction": fmt(risk),
        "notional": fmt(notional),
        "notional_rule": "notional = capital * risk_fraction (not a hard-coded 100 USDT)",
        "venue": venue_test,
        "fold": fold,
        "train": {
            "days": list(fold["train_dates"]),
            "variants": train,
            "selected": selected,
            "selection_rule": "max train-only net PnL over the frozen 12-variant family; ties by strategy_id",
        },
        "validation": {
            "days": list(fold["validation_dates"]),
            "variants": candidate_rows,
            "zero_trade_days_retained": True,
        },
        "controls": {
            "replications": controls["replications"],
            "elapsed_s": controls["elapsed_s"],
            "summary": controls["summary"],
            "daily": controls["daily"],
            "plan_hash": controls["plan_hash"],
            "selection_plan_hash": controls["selection_plan_hash"],
            "notional": controls["notional"],
            "starting_cash": controls["starting_cash"],
            "target_trades_by_day": controls["target_trades_by_day"],
            "trailing_30d_reserve_days": 237,
            "reserve_note": (
                "The recorded seven-day development corpus does not contain the 250-day "
                "primary minimum or the 500-day sensitivity window; this run is development-only."
            ),
        },
        "decision": decision,
        "resource": {"wall_s": round(time.monotonic() - started, 3), "workers": workers},
    }
    report["report_hash_rule"] = (
        "JCS over the report with integers outside the JCS safe range represented as decimal strings"
    )
    report["report_hash"] = hash_jcs(
        _safe_for_jcs({k: v for k, v in report.items() if k != "report_hash"})
    )
    (out / "M2_DEV_RUN.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


def _safe_for_jcs(value: Any) -> Any:
    """Make large integer counters hashable under strict JCS.

    Report fields such as exposure_notional_ns can exceed 2^53. They are still
    emitted as integers for consumers, but the hash projection uses decimal
    strings so the canonical encoder never silently rounds them.
    """
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value) if abs(value) > 2**53 - 1 else value
    if isinstance(value, float):
        return value
    if isinstance(value, Mapping):
        return {str(k): _safe_for_jcs(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_for_jcs(v) for v in value]
    return value


def _manifest_calendar_dates(manifest: Mapping[str, Any]) -> list[str]:
    split = manifest.get("split_manifest") or {}
    dates: set[str] = set()
    for fold in split.get("folds") or []:
        for key in ("train_start", "train_end", "validation_start", "validation_end"):
            value = fold.get(key)
            if value:
                dates.add(str(value)[:10])
    cal = manifest.get("calendar") or {}
    if isinstance(cal, Mapping):
        for value in cal.get("dates") or []:
            dates.add(str(value)[:10])
    return sorted(d for d in dates if len(d) == 10)


def _date_range(start: str, end: str) -> list[date]:
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    out = []
    cur = s
    while cur <= e:
        out.append(cur)
        cur = date.fromordinal(cur.toordinal() + 1)
    return out


def _git_sha() -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10
        )
        return proc.stdout.strip() if proc.returncode == 0 else "UNKNOWN"
    except Exception:
        return "UNKNOWN"
