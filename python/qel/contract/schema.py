"""Runtime schema validation for QEL contracts."""

from __future__ import annotations

import json
import os
import unicodedata
from functools import lru_cache
from pathlib import Path

import jsonschema

from .canonical import CanonicalError, parse_decimal, parse_int128, _DECIMAL_RE, _INT_RE, _UINT_RE


class ValidationError_(ValueError):
    pass


def repo_root() -> Path:
    env = os.environ.get("QEL_ROOT")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "specification" / "schemas" / "runtime.schema.json").exists():
            return parent
    raise RuntimeError("cannot locate QEL repository root")


@lru_cache(maxsize=1)
def runtime_schema() -> dict:
    return json.loads((repo_root() / "specification/schemas/runtime.schema.json").read_text())


@lru_cache(maxsize=1)
def fixture_schema() -> dict:
    return json.loads((repo_root() / "specification/schemas/fixtures.schema.json").read_text())


@lru_cache(maxsize=1)
def architecture() -> dict:
    return json.loads((repo_root() / "specification/spec/architecture.json").read_text())


def _wrapper(def_name: str) -> dict:
    runtime = runtime_schema()
    return {
        "$schema": runtime["$schema"],
        "$id": runtime["$id"],
        "$defs": runtime["$defs"],
        "$ref": f"#/$defs/{def_name}",
    }


def _resolve(schema: dict, root: dict) -> dict:
    seen = 0
    while "$ref" in schema and seen < 32:
        ref = schema["$ref"]
        if not ref.startswith("#/"):
            raise ValidationError_(f"unsupported external ref {ref}")
        node = root
        for part in ref[2:].split("/"):
            node = node[part.replace("~1", "/").replace("~0", "~")]
        schema = node
        seen += 1
    return schema


def _walk_semantics(instance, schema: dict, root: dict, path: str = "$") -> None:
    schema = _resolve(schema, root)

    for key in ("allOf",):
        for sub in schema.get(key, []):
            _walk_semantics(instance, sub, root, path)
    for key in ("anyOf", "oneOf"):
        if key in schema:
            for sub in schema[key]:
                try:
                    _walk_semantics(instance, sub, root, path)
                    break
                except ValidationError_:
                    continue

    if isinstance(instance, dict):
        props = schema.get("properties", {})
        addl = schema.get("additionalProperties", True)
        for k, v in instance.items():
            if not isinstance(k, str):
                raise ValidationError_(f"{path}: non-string key")
            if unicodedata.normalize("NFC", k) != k:
                raise ValidationError_(f"{path}: non-NFC key {k!r}")
            if k in props:
                _walk_semantics(v, props[k], root, f"{path}.{k}")
            elif isinstance(addl, dict):
                _walk_semantics(v, addl, root, f"{path}.{k}")
    elif isinstance(instance, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for i, v in enumerate(instance):
                _walk_semantics(v, item_schema, root, f"{path}[{i}]")
    elif isinstance(instance, str):
        if unicodedata.normalize("NFC", instance) != instance:
            raise ValidationError_(f"{path}: non-NFC text")
        if "x-qel-integer" in schema:
            dom = schema["x-qel-integer"]
            text = instance
            if not (_INT_RE.match(text) or _UINT_RE.match(text)):
                raise ValidationError_(f"{path}: non-canonical integer {text!r}")
            if text == "-0":
                raise ValidationError_(f"{path}: negative zero")
            value = int(text)
            if not int(dom["minimum"]) <= value <= int(dom["maximum"]):
                raise ValidationError_(f"{path}: integer outside declared domain")
        if "x-qel-decimal" in schema:
            if not _DECIMAL_RE.match(instance):
                raise ValidationError_(f"{path}: non-canonical decimal {instance!r}")
            if instance == "-0" or (instance.startswith("-0.") and set(instance.split(".")[1]) == {"0"}):
                raise ValidationError_(f"{path}: negative zero")
            parse_decimal(instance)
    elif isinstance(instance, float):
        raise ValidationError_(f"{path}: floating point forbidden in contract identity/config")


def validate_def(def_name: str, instance) -> None:
    runtime = runtime_schema()
    wrapper = _wrapper(def_name)
    validator = jsonschema.Draft202012Validator(wrapper)
    errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.path))
    if errors:
        raise ValidationError_(f"{def_name}: {errors[0].message}")
    _walk_semantics(instance, wrapper, wrapper)


def validate_fixtures(document: dict) -> None:
    schema = fixture_schema()
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(document), key=lambda e: list(e.path))
    if errors:
        raise ValidationError_(f"fixtures: {errors[0].message}")


def api_receipt(request_id: str, resource_id: str, ledger_event_hash: str, state: str, generation: int) -> dict:
    receipt = {
        "request_id": request_id,
        "resource_id": resource_id,
        "ledger_event_hash": ledger_event_hash,
        "state": state,
        "generation": generation,
    }
    validate_def("ApiReceipt", receipt)
    return receipt
