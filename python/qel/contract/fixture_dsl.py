"""QEL_FIXTURE_DSL_1 translator contract.

The fixture DSL is a test input language, not a production strategy language.
This module validates and normalises fixture documents against the published
schema and enumerates operations; execution semantics live in the conformance
harness.
"""

from __future__ import annotations

import json
from typing import Iterator

from .schema import fixture_schema, repo_root, validate_fixtures
from .canonical import CanonicalError


class FixtureDslError(ValueError):
    pass


def load_document(path: str | None = None) -> dict:
    root = repo_root() / "specification/fixtures/conformance.json" if path is None else None
    doc = json.loads((root or path).read_text())
    validate_fixtures(doc)
    return doc


def operations() -> dict[str, dict]:
    """Map operation name -> {args: [...], required: [...], variants: n}."""
    schema = fixture_schema()
    steps = schema["properties"]["fixtures"]["items"]["properties"]["scenarios"]["items"]["properties"]["steps"]["items"]["oneOf"]
    out: dict[str, dict] = {}
    for variant in steps:
        op = variant["properties"]["operation"]["const"]
        args = variant["properties"]["arguments"]
        variants = args.get("anyOf", [args])
        entry = out.setdefault(op, {"args": set(), "required": [], "variants": len(variants)})
        for v in variants:
            entry["args"].update(v.get("properties", {}))
            if not entry["required"]:
                entry["required"] = list(v.get("required", []))
    return {k: {"args": sorted(v["args"]), "required": v["required"], "variants": v["variants"]} for k, v in sorted(out.items())}


def iter_scenarios(doc: dict) -> Iterator[tuple[dict, dict]]:
    for fixture in doc["fixtures"]:
        for scenario in fixture["scenarios"]:
            yield fixture, scenario


def translate(doc: dict) -> list[dict]:
    """Return immutable, normalised scenario records for the harness.

    Unknown operations are rejected. Setup fields are schema-limited, so no
    undeclared default can be introduced by the adapter.
    """
    validate_fixtures(doc)
    known = operations()
    records = []
    for fixture, scenario in iter_scenarios(doc):
        steps = []
        for i, step in enumerate(scenario["steps"]):
            op = step["operation"]
            if op not in known:
                raise FixtureDslError(f"{fixture['id']}: unknown operation {op}")
            args = step["arguments"]
            # Variant-specific required sets were already enforced by the
            # published fixtures schema; no hidden default is introduced here.
            unknown = set(args) - set(known[op]["args"])
            if unknown:
                raise FixtureDslError(f"{fixture['id']}/{scenario['name']}: {op} unknown args {sorted(unknown)}")
            steps.append(
                {
                    "index": i,
                    "at_ns": int(step["at_ns"]),
                    "operation": op,
                    "arguments": dict(args),
                }
            )
        records.append(
            {
                "fixture_id": fixture["id"],
                "kind": fixture["kind"],
                "scenario": scenario["name"],
                "setup": dict(scenario["setup"]),
                "steps": steps,
                "expected": scenario["expected"],
                "oracle_source": fixture.get("oracle_source"),
            }
        )
    return records


def validate_and_translate(path: str | None = None) -> list[dict]:
    return translate(load_document(path))
