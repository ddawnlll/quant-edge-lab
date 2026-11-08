"""End-to-end sealed evaluator exercise through the public component methods."""

from __future__ import annotations

import os
import random

from .core import SealedError, SealedRegistry


def run_demo(root: str) -> dict:
    reg = SealedRegistry(root)
    # This is an evaluator calibration vector, not a market result.
    rng = random.Random(20261108)
    candidate = [0.004 + rng.gauss(0, 0.002) for _ in range(60)]
    control = [rng.gauss(0, 0.002) for _ in range(60)]

    pre_consumption = None
    try:
        reg.decide("COHORT-E2E", candidate, control, capital=1000, economic_minimum=0.0001, b=64)
    except SealedError as exc:
        pre_consumption = exc.code

    freeze = reg.freeze_cohort(
        "COHORT-E2E",
        "PROG-E2E",
        0,
        ["a" * 64, "b" * 64],
        "c" * 64,
        "d" * 64,
        "e" * 64,
        ["f" * 64],
    )
    unacknowledged = None
    try:
        reg.spend_consumption("COHORT-E2E", acknowledged=False)
    except SealedError as exc:
        unacknowledged = exc.code
    spend = reg.spend_consumption("COHORT-E2E", acknowledged=True)
    decision = reg.decide("COHORT-E2E", candidate, control, capital=1000, economic_minimum=0.0001, b=128)
    verified = reg.verify(decision)
    second_decision = None
    try:
        reg.decide("COHORT-E2E", candidate, control, capital=1000, economic_minimum=0.0001, b=64)
    except SealedError as exc:
        second_decision = exc.code

    return {
        "status": "PASS" if verified and pre_consumption == "WAITING_AUTHORIZED_COHORT" else "FAIL",
        "cohort": freeze,
        "consumption": spend,
        "pre_consumption_refusal": pre_consumption,
        "unacknowledged_refusal": unacknowledged,
        "second_decision_refusal": second_decision,
        "decision": {
            "verdict": decision["verdict"],
            "p_profit": decision["p_profit"],
            "p_control": decision["p_control"],
            "p_iut": decision["p_iut"],
            "signature": decision["signature"],
            "public_key": decision["public_key"],
        },
        "signature_verified": verified,
        "key_mode": oct(os.stat(reg.key_path).st_mode & 0o777),
        "db_mode": oct(os.stat(reg.db_path).st_mode & 0o777),
        "data_boundary": "synthetic evaluator calibration vector, not a market result",
    }
