"""Run a bounded paired statistics job from a daily-return JSON input.

The job is a thin production adapter over :func:`qel.stats.core.paired_decision`:
it feeds the candidate and matched control through the same shared-calendar,
shared-bootstrap-index, IUT and Holm procedure used by the research report. It
never bootstraps the control against zero, so a candidate that merely beats zero
cannot pass while a stronger matched control beats it.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

from .core import DecisionError, paired_decision

_DEFAULT_START = "2026-01-01"
_DEFAULT_SYMBOL = "BTC"


def _calendar(n: int, start_date: str) -> list[str]:
    start = datetime.date.fromisoformat(start_date)
    return [(start + datetime.timedelta(days=i)).isoformat() for i in range(n)]


def _panel(values: list, dates: list[str], symbol: str) -> dict:
    if len(values) != len(dates):
        raise DecisionError("MISSING_DATA", "candidate/control length mismatch")
    return {symbol: {day: str(value) for day, value in zip(dates, values)}}


def _blocked(code: str, data_boundary: str) -> dict:
    return {
        "status": "BLOCKED",
        "verdict": "INSUFFICIENT_EVIDENCE",
        "error": code,
        "reason_codes": [code],
        "p_profit_upper": None,
        "p_control_upper": None,
        "p_iut": None,
        "adjusted_p": None,
        "mean_daily_return": None,
        "mean_excess_return": None,
        "ci_low": None,
        "ci_high": None,
        "b": None,
        "data_boundary": data_boundary,
    }


def run_job(path: str, b: int = 256) -> dict:
    """Run one paired decision job.

    The returned ``verdict`` is the faithful paired verdict: it may be
    INSUFFICIENT_EVIDENCE when ``b`` is below the frozen requirement, or
    WAITING_COMPUTE_BUDGET when the declared envelope cannot resolve the family.
    ``status`` is the job-execution status (PASS/WAITING/BLOCKED for structural
    input failures), while ``decision_status`` carries the raw decision status.
    A payload may raise the budget via ``b``/``max_resamples``.
    """
    payload = json.loads(Path(path).read_text())
    data_boundary = payload.get("data_boundary", "caller-supplied returns")
    capital = payload.get("capital", "1000")
    economic_floor = payload.get("economic_minimum", "0.0001")
    control_floor = payload.get("control_minimum", payload.get("control_floor", "0"))

    if "candidate" in payload and "control" in payload:
        candidate = payload["candidate"]
        control = payload["control"]
    else:
        candidate_daily = payload.get("candidate_daily") or []
        control_daily = payload.get("control_daily") or []
        if len(candidate_daily) < 3 or len(control_daily) < 3:
            return _blocked("INSUFFICIENT_HISTORY", data_boundary)
        if len(candidate_daily) != len(control_daily):
            return _blocked("MISSING_DATA", data_boundary)
        dates = _calendar(len(candidate_daily), payload.get("start_date", _DEFAULT_START))
        symbol = payload.get("symbol", _DEFAULT_SYMBOL)
        try:
            candidate = _panel(candidate_daily, dates, symbol)
            control = _panel(control_daily, dates, symbol)
        except DecisionError as exc:
            return _blocked(exc.code, data_boundary)

    try:
        result = paired_decision(
            candidate,
            control,
            str(capital),
            str(economic_floor),
            str(control_floor),
            alpha=float(payload.get("alpha", 0.025)),
            block=int(payload.get("block", 5)),
            block_sensitivity=int(payload.get("block_sensitivity", 10)),
            hac_lag=int(payload.get("hac_lag", 5)),
            b=int(payload.get("b", b)),
            max_resamples=int(payload.get("max_resamples", 1_000_000)),
            seed=int(payload.get("seed", 0)),
            family_size=int(payload.get("family_size", 1)),
            family_index=int(payload.get("family_index", 0)),
            power_target=float(payload.get("power", 0.8)),
        )
    except DecisionError as exc:
        return _blocked(exc.code, data_boundary)

    primary = result.get("primary")
    endpoint_a = primary["endpoints"]["A"] if primary else None
    endpoint_b = primary["endpoints"]["B"] if primary else None
    verdicted = result["verdict"]
    return {
        "status": "WAITING" if verdicted == "WAITING_COMPUTE_BUDGET" else "PASS",
        "decision_status": result["status"],
        "verdict": verdicted,
        "reason_codes": result.get("reason_codes", []),
        "p_profit_upper": endpoint_a["p_upper"] if endpoint_a else None,
        "p_control_upper": endpoint_b["p_upper"] if endpoint_b else None,
        "p_iut": result.get("iut_p_upper"),
        "adjusted_p": result.get("adjusted_p"),
        "mean_daily_return": endpoint_a["mean_daily_return"] if endpoint_a else None,
        "mean_excess_return": endpoint_b["mean_daily_return"] if endpoint_b else None,
        "ci_low": endpoint_a["ci_lower_95"] if endpoint_a else None,
        "ci_high": endpoint_a["ci_upper_95"] if endpoint_a else None,
        "b": result["budget"]["b_used"] if result.get("budget") else None,
        "data_boundary": data_boundary,
        "method_hash": result.get("method_hash"),
        "budget": result.get("budget"),
        "decision": result,
    }
