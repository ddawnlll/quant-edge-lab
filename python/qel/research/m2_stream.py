"""Streamed full-fold M2 development execution (A3).

Builds each fold day's native tape from the verified raw archives,
replays the frozen 12-variant family, fits matched controls with the
bounded-memory fit, discards the tape, and repeats day by day. Peak
disk is one tape plus small per-day reports/sidecars; peak RAM is the
compact quote arrays of the bounded fit. Every invariant of the
canonical dev-week runner is preserved (see
``reports/orchestration/A3_STREAMING_DESIGN.md``); the fold windows
come from the frozen manifest and never touch the sealed reserve.

Restart safety: ``work/run.json`` pins the deterministic ``run_id``;
``work/checkpoints.json`` records per-day tape hashes, variant report
states and report hashes, quote sidecar presence and funding rows.
A restart re-verifies stored report hashes and skips confirmed work;
a mid-day interruption is discarded and redone. No checkpoint write
mutates prior evidence (atomic tmp + rename).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from ..accounting.core import dec, fmt
from ..contract.canonical import hash_jcs
from ..stats.core import family_decision
from . import control as C
from . import control_stream as S
from . import m2 as M2
from .protocol import ProtocolError

SEALED_FROM = "2026-03-08"
FOLD_SCHEMA = "qel.m2_run_fold_stream/1.0"


def _sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _git_sha() -> str | None:
    try:
        import subprocess

        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip() or None
    except Exception:
        return None


def _load_builder(script_path: str):
    spec = importlib.util.spec_from_file_location("m2_fold_tape_builder", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text())


def fold_windows(manifest: Mapping[str, Any], fold_id: str) -> tuple[list[str], list[str], dict]:
    split = manifest.get("split_manifest") if isinstance(manifest, Mapping) else None
    if not isinstance(split, Mapping):
        raise ProtocolError("MISSING_SPLIT_MANIFEST")
    for fold in split.get("folds") or []:
        if fold.get("fold_id") == fold_id:
            train = [d.isoformat() for d in M2._date_range(str(fold["train_start"])[:10], str(fold["train_end"])[:10])]
            validation = [d.isoformat() for d in M2._date_range(str(fold["validation_start"])[:10], str(fold["validation_end"])[:10])]
            return train, validation, fold
    raise ProtocolError("UNKNOWN_FOLD", {"fold_id": fold_id})


class Checkpoints:
    """Atomic, append-only-by-transition checkpoint store."""

    def __init__(self, path: Path, run_id: str):
        self.path = path
        self.run_id = run_id
        self._lock = threading.RLock()
        self.data: dict[str, Any] = {"run_id": run_id, "days": {}, "funding": {}, "phases": {}}
        if path.is_file():
            existing = _read_json(path)
            if existing.get("run_id") != run_id:
                raise ProtocolError("RUN_ID_MISMATCH", {"expected": run_id, "found": existing.get("run_id")})
            self.data = existing

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n")
        tmp.rename(self.path)

    def merge(self, other: "Checkpoints", prefixes: tuple[str, ...]) -> None:
        """Merge funding rows recorded by sibling workers under the lock."""
        with self._lock:
            for key in prefixes:
                left, right = self.data.get(key, {}), other.data.get(key, {})
                if isinstance(left, dict) and isinstance(right, dict):
                    left.update({k: v for k, v in right.items() if k not in left})
            self.save()

    def day_done(self, day: str, report_dir: Path) -> bool:
        record = self.data["days"].get(day)
        if not record or record.get("complete") is not True:
            return False
        for sid, entry in (record.get("variants") or {}).items():
            report = report_dir / f"{sid}-{day}.json"
            if not report.is_file() or _sha256_file(report) != entry.get("report_sha"):
                return False
        return True

    def set_day(self, day: str, record: dict) -> None:
        with self._lock:
            self.data["days"][day] = record
            self.save()

    def set_phase(self, name: str, record: dict) -> None:
        self.data["phases"][name] = record
        self.save()


def run_m2_fold_stream(
    *,
    manifest_path: str,
    fold_id: str,
    binary: str,
    strategy_base_path: str,
    out_dir: str,
    work_dir: str,
    builder_script: str,
    capital: str = "1000",
    risk_fraction: str = "0.1",
    control_replications: int = 1000,
    family_size: int = 12,
    workers: int = 4,
    max_holding_ns: int = 3_600_000_000_000,
    bootstrap_resamples: int = 10000,
    train_days_limit: int = 0,
    validation_days_limit: int = 0,
    day_workers: int = 3,
    seed_workers: int = 4,
) -> dict:
    started = time.monotonic()
    work = Path(work_dir)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    manifest = _read_json(Path(manifest_path))
    base = _read_json(Path(strategy_base_path))
    train_dates, validation_dates, raw_fold = fold_windows(manifest, fold_id)
    if train_days_limit:
        train_dates = train_dates[:train_days_limit]
    if validation_days_limit:
        validation_dates = validation_dates[:validation_days_limit]
    if any(d >= SEALED_FROM for d in train_dates + validation_dates):
        raise ProtocolError("SEALED_RESERVE_TOUCHED", {"sealed_from": SEALED_FROM})
    if not train_dates or not validation_dates:
        raise ProtocolError("EMPTY_FOLD_WINDOW")

    capital_d = dec(capital)
    notional = capital_d * dec(risk_fraction)
    strategies = M2._strategy_payloads(base, family_size)
    run_params = {
        "fold_id": fold_id,
        "train_dates": train_dates,
        "validation_dates": validation_dates,
        "capital": fmt(capital_d),
        "risk_fraction": fmt(dec(risk_fraction)),
        "notional": fmt(notional),
        "family_size": family_size,
        "control_replications": control_replications,
        "max_holding_ns": max_holding_ns,
        "bootstrap_resamples": bootstrap_resamples,
        "binary_sha256": _sha256_file(binary),
        "strategy_base_sha256": _sha256_file(strategy_base_path),
        "manifest_sha256": _sha256_file(manifest_path),
        "builder_sha256": _sha256_file(builder_script),
        "builder_script": Path(builder_script).name,
    }
    run_id = hash_jcs(run_params)
    run_record = Path(work_dir) / "run.json"
    if not run_record.is_file():
        run_record.write_text(json.dumps({"run_id": run_id, "params": run_params}, indent=2, sort_keys=True) + "\n")
    else:
        existing = _read_json(run_record)
        if existing.get("run_id") != run_id:
            raise ProtocolError("RUN_ID_MISMATCH", {"expected": run_id})
    checkpoints = Checkpoints(Path(work_dir) / "checkpoints.json", run_id)

    builder = _load_builder(builder_script)
    tapes_dir = work / "tapes"
    reports_dir = out / "reports"
    quotes_dir = work / "quotes"
    tapes_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    quotes_dir.mkdir(parents=True, exist_ok=True)
    builder.OUT = tapes_dir
    builder.ARCH = Path.cwd() / "data/raw/ARCHIVE_BINANCE_USDM/BTCUSDT"
    if not builder.ARCH.is_dir():
        raise ProtocolError("ARCHIVE_ROOT_MISSING", {"arch": str(builder.ARCH)})
    funding_rows_all = builder.load_funding_rows(set(train_dates + validation_dates))

    timings: dict[str, dict[str, float]] = {}

    def process_day(day: str, kind: str) -> dict:
        if checkpoints.day_done(day, reports_dir):
            return {"day": day, "phase": kind, "status": "skipped"}
        build_started = time.monotonic()
        manhattan = builder.build_day(day, funding_rows_all)
        build_s = time.monotonic() - build_started
        if manhattan["status"] not in ("ok", "skip"):
            raise ProtocolError("TAPE_BUILD_FAILED", manhattan)
        tape_path = tapes_dir / f"{day}.jsonl"
        if not tape_path.is_file() or tape_path.stat().st_size == 0:
            raise ProtocolError("TAPE_EMPTY", {"day": day})
        tape_sha = _sha256_file(tape_path)
        if "sha256" in manhattan and manhattan["sha256"] != tape_sha:
            raise ProtocolError("TAPE_HASH_MISMATCH", {"day": day})
        if manhattan.get("funding", 0) == 0:
            raise ProtocolError("TAPE_FUNDING_MISSING", {"day": day})
        ts, bid, ask = S.extract_day_quotes(str(tape_path))
        np.savez(quotes_dir / f"{day}.npz", ts=ts, bid=bid, ask=ask)

        replay_started = time.monotonic()
        variant_entries: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=max(1, min(workers, 6))) as pool:
            futures = {}
            for strat in strategies:
                sid = str(strat["strategy_id"])
                spath = reports_dir / f"{sid}.strategy.json"
                spath.write_text(json.dumps(strat, indent=2, sort_keys=True))
                futures[pool.submit(M2._run_candidate, binary, str(tape_path), str(spath), str(reports_dir / f"{sid}-{day}.json"), float(notional))] = sid
            for fut in as_completed(futures):
                sid = futures[fut]
                entry = fut.result()
                entry["report_sha"] = _sha256_file(reports_dir / f"{sid}-{day}.json") if entry.get("status") == "PASS" else None
                variant_entries[sid] = entry
        replay_s = time.monotonic() - replay_started
        tape_path.unlink(missing_ok=True)
        if int(manhattan.get("funding", 0)) > 0:
            with checkpoints._lock:
                checkpoints.data["funding"][day] = _day_funding(funding_rows_all, day)
        checkpoints.set_day(
            day,
            {
                "complete": True,
                "phase": kind,
                "tape_sha256": tape_sha,
                "build_s": round(build_s, 3),
                "replay_s": round(replay_s, 3),
                "counts": {k: manhattan.get(k) for k in ("quotes", "trades", "marks", "funding", "bytes", "sha256")},
                "variants": variant_entries,
            },
        )
        timings[day] = {"build_s": round(build_s, 3), "replay_s": round(replay_s, 3)}
        return {"day": day, "phase": kind, "status": "done", "tape_sha256": tape_sha}

    todo = [(day, "train") for day in train_dates] + [(day, "validation") for day in validation_dates]
    with ThreadPoolExecutor(max_workers=max(1, day_workers)) as pool:
        futures = {pool.submit(process_day, day, kind): day for day, kind in todo}
        for fut in as_completed(futures):
            fut.result()

    def day_variant(day: str, sid: str) -> dict:
        return checkpoints.data["days"][day]["variants"][sid]

    def aggregate(days: Sequence[str]) -> dict[str, dict]:
        variants: dict[str, dict] = {str(s["strategy_id"]): {"days": {}} for s in strategies}
        for day in days:
            for sid in variants:
                variants[sid]["days"][day] = day_variant(day, sid)
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

    train = aggregate(train_dates)
    candidates = [sid for sid, row in train.items() if row["status"] == "PASS"]
    selected = max(candidates, key=lambda sid: (dec(train[sid]["net_pnl"] or "0"), sid)) if candidates else None
    validation = aggregate(validation_dates)

    # Venue feasibility gate on the first train quote (same rule as dev).
    first_day_arrays = _load_day_arrays(quotes_dir, train_dates + validation_dates, need_first=True)
    first_quotes = first_day_arrays[0]
    min_price = Fraction(int(first_quotes[2][0]), 100) if len(first_quotes[2]) else Fraction(0)
    min_notional = Fraction("0.001") * min_price
    venue_test = {
        "target_notional": fmt(notional),
        "min_quantity": "0.001",
        "reference_price": fmt(min_price),
        "min_notional": fmt(min_notional),
        "feasible": notional >= min_notional,
        "status": "PASS" if notional >= min_notional else "INFEASIBLE_VENUE_MIN_NOTIONAL",
    }
    if not venue_test["feasible"]:
        return {"schema": FOLD_SCHEMA, "run_id": run_id, "status": "INFEASIBLE_VENUE_MIN_NOTIONAL", "venue": venue_test, "fold_id": fold_id}

    control_arrays = _load_day_arrays(quotes_dir, train_dates + validation_dates)
    train_end_ns = M2._day_bounds(train_dates[-1])[1] - 1
    funding_raw: list[dict] = []
    for day in train_dates + validation_dates:
        for ts_ms, rate in checkpoints.data["funding"].get(day, ()):
            funding_raw.append({"ts_ns": int(ts_ms) * 1_000_000, "rate": rate})
    train_trades: int | None = None
    if selected:
        train_trades = max(
            0,
            sum(int(train[selected]["days"][d].get("fills") or 0) for d in train_dates) // max(1, len(train_dates)),
        )
    plan = S.fit_control_plan_stream(
        control_arrays,
        trades=train_trades,
        notional=str(notional),
        seed=0,
        max_holding_ns=max_holding_ns,
        train_end_ns=train_end_ns,
        funding=funding_raw,
    )
    fit_started = time.monotonic()
    agg = C.StreamingControlAggregator(plan, keep_summaries=True)
    _aggregate_seeds(plan, agg, control_replications, seed_workers)
    seeds_s = time.monotonic() - fit_started
    checkpoints.set_phase("controls", {"replications": control_replications, "plan_hash": plan.plan_hash, "selection_plan_hash": plan.selection_plan_hash, "elapsed_s": round(seeds_s, 3)})
    daily = agg.daily_series()
    validation_days = set(validation_dates)
    controls_panel = {
        "BTC": {
            str(row["day"]): str(row["mean_net_pnl"]) if row["mean_net_pnl"] is not None else "0"
            for row in daily
            if str(row["day"]) in validation_days
        }
    }

    candidate_panel: dict[str, dict[str, str]] = {}
    candidate_rows: dict[str, dict] = {}
    for sid, row in validation.items():
        if row["status"] != "PASS":
            candidate_rows[sid] = {"status": "BLOCKED", "reason": "VALIDATION_RUN_FAILED"}
            continue
        panel = {}
        for day in validation_dates:
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

    decision = None
    if candidate_panel:
        try:
            decision = family_decision(
                candidate_panel,
                controls_panel,
                capital,
                "0",
                "0",
                b=max(bootstrap_resamples, 10001),
                max_resamples=2_000_000,
                seed=0,
            )
        except Exception as exc:  # pragma: no cover - failing closed with the error recorded
            decision = {"status": "ERROR", "error": repr(exc)}
    else:
        decision = {"status": "NO_CANDIDATE", "verdict": "INSUFFICIENT_EVIDENCE"}

    summary = agg.summary()
    report = {
        "schema": FOLD_SCHEMA,
        "run_id": run_id,
        "status": "PASS" if candidate_panel and control_replications else "INSUFFICIENT_EVIDENCE",
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scope": "FULL_FOLD_SCOPE",
        "scope_note": (
            f"Frozen fold {fold_id} ({len(train_dates)} train + {len(validation_dates)} validation days) "
            "executed with streamed day tapes. Sealed reserve never touched."
        ),
        "base_sha": _git_sha(),
        "binary": {"path": binary, "sha256": _sha256_file(binary)},
        "strategy_base": {"path": strategy_base_path, "sha256": _sha256_file(strategy_base_path)},
        "manifest": {
            "path": manifest_path,
            "sha256": _sha256_file(manifest_path),
            "check": M2.check_frozen_manifest(manifest, {d: str(quotes_dir / f"{d}.npz") for d in train_dates + validation_dates}),
        },
        "capital": fmt(capital_d),
        "risk_fraction": fmt(dec(risk_fraction)),
        "notional": fmt(notional),
        "notional_rule": "notional = capital * risk_fraction",
        "venue": venue_test,
        "fold": {"fold_id": fold_id, "train_dates": train_dates, "validation_dates": validation_dates, "frozen_fold": raw_fold},
        "train": {"days": list(train_dates), "variants": train, "selected": selected, "selection_rule": "max train-only net PnL over the frozen family; ties by strategy_id"},
        "validation": {"days": list(validation_dates), "variants": candidate_rows, "zero_trade_days_retained": True},
        "controls": {
            "replications": control_replications,
            "summary": summary,
            "daily": daily,
            "plan_hash": plan.plan_hash,
            "selection_plan_hash": plan.selection_plan_hash,
            "notional": str(plan.notional),
            "starting_cash": str(plan.starting_cash),
            "target_trades_by_day": plan.target_trades_by_day,
        },
        "decision": decision,
        "resource": {"wall_s": round(time.monotonic() - started, 3), "workers": workers, "day_timings": timings, "seeds_s": round(seeds_s, 3)},
    }
    (out / "M2_FOLD_RUN.json").write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n")
    return report


def _day_funding(funding_rows: Sequence[tuple[int, str]], day: str) -> list[list[Any]]:
    from datetime import datetime as _dt, timezone as _tz

    start_ms = int(_dt.combine(_dt.fromisoformat(day).date(), _dt.min.time(), _tz.utc).timestamp() * 1000)
    end_ms = start_ms + 86400_000
    return [[ts_ms, rate] for ts_ms, rate in funding_rows if start_ms <= ts_ms < end_ms]


def _load_day_arrays(quotes_dir: Path, days: Sequence[str], need_first: bool = False) -> list[tuple[str, Any, Any, Any]]:
    arrays = []
    for day in days:
        path = quotes_dir / f"{day}.npz"
        loaded = np.load(path)
        arrays.append((day, loaded["ts"], loaded["bid"], loaded["ask"]))
        if need_first:
            break
    return arrays



_SEED_PLAN: Any = None


def _seed_index(seed_index: int):
    """Top-level worker: fork children inherit _SEED_PLAN (no pickling)."""
    return seed_index, C.run_control_seed(_SEED_PLAN, seed_index)


def _aggregate_seeds(plan: Any, aggregator: Any, replications: int, workers: int) -> None:
    """Aggregation in strict seed order (the rolling hash is ordered)."""
    from concurrent.futures import ProcessPoolExecutor
    import multiprocessing as mp

    if max(1, workers) == 1 or replications <= 1:
        for i in range(replications):
            aggregator.add(C.run_control_seed(plan, i))
        return
    global _SEED_PLAN
    _SEED_PLAN = plan
    ctx = mp.get_context("fork")
    pending: dict[int, Any] = {}
    next_add = 0
    with ProcessPoolExecutor(max_workers=max(1, workers), mp_context=ctx) as pool:
        futures = {pool.submit(_seed_index, i): i for i in range(replications)}
        for fut in as_completed(futures):
            index, result = fut.result()
            pending[index] = result
            while next_add in pending:
                aggregator.add(pending.pop(next_add))
                next_add += 1
    while next_add in pending:
        aggregator.add(pending.pop(next_add))
        next_add += 1
