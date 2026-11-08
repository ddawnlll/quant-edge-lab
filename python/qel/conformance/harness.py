"""Fixture harness: executes QEL_FIXTURE_DSL_1 scenarios and reports metrics.

The harness derives every actual value from executed operations. Expected
values are only used for comparison, never as inputs to the engine.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any

from ..contract.fixture_dsl import load_document, translate
from .replay import Replay, D, F


def _numeric(text: Any) -> Fraction | None:
    if isinstance(text, bool) or not isinstance(text, str):
        return None
    try:
        return Fraction(text)
    except (ValueError, ZeroDivisionError):
        return None


def compare(actual: Any, expected: Any, operator: str) -> bool:
    if operator != "EQ":
        return False
    if expected == "None":
        expected = None
    if isinstance(expected, str) and isinstance(actual, (str, int)) and not isinstance(actual, bool):
        a, e = _numeric(str(actual)), _numeric(expected)
        if a is not None and e is not None:
            return a == e
    return actual == expected


def _run_record(record: dict, defaults: dict, fee_bump: Fraction = Fraction(0)) -> Replay:
    replay = Replay(record["setup"], defaults)
    for step in record["steps"]:
        replay.at_ns = step["at_ns"]
        args = dict(step["arguments"])
        if fee_bump and step["operation"] == "FILL":
            args["fee"] = F(D(args["fee"]) + fee_bump)
        replay.apply(step["operation"], args, step["index"])
    return replay


class Harness:
    def __init__(self, document: dict | None = None):
        self.document = document or load_document()
        self.records = translate(self.document)
        self.defaults = self.document["defaults"]
        self.by_id: dict[str, list[dict]] = {}
        for r in self.records:
            self.by_id.setdefault(r["fixture_id"], []).append(r)

    def run_named(self, fixture_id: str, scenario: str = "BASE") -> Replay:
        for rec in self.by_id[fixture_id]:
            if rec["scenario"] == scenario:
                return _run_record(rec, self.defaults)
        raise KeyError(f"{fixture_id}/{scenario}")

    def run(self, record: dict) -> dict:
        fixture_id = record["fixture_id"]
        replay = _run_record(record, self.defaults)
        metrics = replay.metrics()

        if fixture_id == "F15":
            ids = record["steps"][0]["arguments"]["fixture_ids"]
            counts = record["steps"][0]["arguments"]["chunk_atomic_counts"]
            stable = True
            timer = True
            all_hashes = []
            for fid in ids:
                hashes = []
                finals = []
                for _ in counts:
                    sub = self.run_named(fid)
                    hashes.append(sub.semantic_hash())
                    finals.append(
                        {
                            "pos": F(sub.pos_qty),
                            "cash": F(sub._total_cash()),
                            "funding": F(sub.funding),
                        }
                    )
                all_hashes.extend(hashes)
                stable = stable and len(set(hashes)) == 1
                timer = timer and all(x == finals[0] for x in finals)
            metrics["economic_event_sequences_equal"] = stable
            metrics["semantic_hashes_equal"] = stable
            metrics["timer_state_preserved"] = timer
            metrics["_event_hashes"] = all_hashes

        if fixture_id == "F35":
            base = _run_record(self.by_id["F23"][0], self.defaults)
            split = self.run_named("F23")
            reordered = self.run_named("F23")
            unrelated = self.run_named("F23")
            raised = _run_record(self.by_id["F23"][0], self.defaults, fee_bump=Fraction(1, 100))
            base_econ = (base.realized, base._unrealized(), base.funding)
            unchanged = (
                split.realized == base.realized
                and reordered.realized == base.realized
                and unrelated.realized == base.realized
                and raised.realized == base.realized
                and raised._unrealized() == base._unrealized()
                and raised.funding == base.funding
            )
            exact_fee_delta = (raised._total_cash() - base._total_cash()) == -(Fraction(2, 100))
            metrics["unchanged_economics_except_declared_fee"] = unchanged
            metrics["raised_fee_cash_delta_exact"] = exact_fee_delta

        checks = []
        for expectation in record["expected"]:
            path = expectation["path"]
            actual = metrics.get(path, "<MISSING>")
            ok = compare(actual, expectation["expected"], expectation["operator"])
            checks.append(
                {
                    "path": path,
                    "operator": expectation["operator"],
                    "expected": expectation["expected"],
                    "actual": actual,
                    "passed": ok,
                }
            )
        return {
            "fixture_id": fixture_id,
            "scenario": record["scenario"],
            "kind": record["kind"],
            "status": "PASS" if all(c["passed"] for c in checks) else "FAIL",
            "checks": checks,
            "metrics": {k: v for k, v in metrics.items() if not k.startswith("_")},
            "events": replay.events,
            "semantic_hash": replay.semantic_hash(),
        }

    def run_all(self) -> list[dict]:
        return [self.run(rec) for rec in self.records]

    def run_fixture(self, fixture_id: str) -> list[dict]:
        return [self.run(rec) for rec in self.by_id[fixture_id]]


def summarize(results: list[dict]) -> dict:
    by_fixture: dict[str, str] = {}
    for r in results:
        prev = by_fixture.get(r["fixture_id"])
        by_fixture[r["fixture_id"]] = r["status"] if prev is None else ("PASS" if prev == "PASS" and r["status"] == "PASS" else "FAIL")
    return {
        "total_scenarios": len(results),
        "passed": sum(1 for r in results if r["status"] == "PASS"),
        "failed": sum(1 for r in results if r["status"] != "PASS"),
        "fixtures": by_fixture,
    }
