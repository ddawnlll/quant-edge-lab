"""Run a bounded statistics job from a daily-return JSON input."""

from __future__ import annotations

import json
from pathlib import Path

from .core import classify, iut, studentized_bootstrap_p


def run_job(path: str, b: int = 256) -> dict:
    payload = json.loads(Path(path).read_text())
    candidate = [float(x) for x in payload["candidate_daily"]]
    control = [float(x) for x in payload["control_daily"]]
    capital = float(payload.get("capital", 1000))
    economic_minimum = float(payload.get("economic_minimum", 0.0001))
    if len(candidate) < 3 or len(control) < 3:
        return {"status": "BLOCKED", "reason": "INSUFFICIENT_HISTORY"}
    ret_c = [x / capital for x in candidate]
    ret_k = [x / capital for x in control]
    pc = studentized_bootstrap_p(ret_c, b=b, seed=1)
    pk = studentized_bootstrap_p(ret_k, b=b, seed=2)
    p = iut(pc.p_upper, pk.p_upper)
    mean = sum(ret_c) / len(ret_c)
    ci_low = mean - 1.96 * pc.se
    verdict = classify(p, float(payload.get("power", 0.8)), ci_low, mean + 1.96 * pc.se, economic_minimum)
    return {
        "status": "PASS",
        "verdict": verdict,
        "p_profit_upper": pc.p_upper,
        "p_control_upper": pk.p_upper,
        "p_iut": p,
        "mean_daily_return": mean,
        "ci_low": ci_low,
        "b": b,
        "data_boundary": payload.get("data_boundary", "caller-supplied returns"),
    }
