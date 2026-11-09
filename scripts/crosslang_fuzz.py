#!/usr/bin/env python3
"""Cross-language randomized canonical JSON vectors and rejection cases."""

from __future__ import annotations

import json
import random
import string
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from qel.contract.canonical import CanonicalError, canonical_bytes  # noqa: E402
from qel.contract.runkey import RUN_KEY_OBJECT_KEYS, run_key  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
GO = ROOT / "bin/qel-contract"
RUST = ROOT / "bin/qel-contract-rs"


def random_value(rng: random.Random, depth: int = 0):
    choices = ["int", "str", "bool", "null", "list", "dict"]
    kind = rng.choice(choices if depth < 3 else choices[:4])
    if kind == "int":
        return rng.randint(-9_007_199_254_740_991, 9_007_199_254_740_991)
    if kind == "str":
        alphabet = string.ascii_letters + string.digits + "éμ𝄞\u0001\t\n\"\\"
        return "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12)))
    if kind == "bool":
        return rng.random() < 0.5
    if kind == "null":
        return None
    if kind == "list":
        return [random_value(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    return {f"k{rng.randint(0, 20)}_{rng.choice(string.ascii_letters)}": random_value(rng, depth + 1) for _ in range(rng.randint(0, 4))}


def run(binary: Path, value) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(binary), "canonical"],
        input=json.dumps(value, ensure_ascii=False),
        text=True,
        capture_output=True,
        env={"QEL_ROOT": str(ROOT)},
    )

def runkey(binary: Path, value) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(binary), "runkey"],
        input=json.dumps(value, ensure_ascii=False),
        text=True,
        capture_output=True,
        env={"QEL_ROOT": str(ROOT)},
    )


def main() -> int:
    rng = random.Random(20261108)
    mismatches = []
    for i in range(120):
        value = random_value(rng)
        py = canonical_bytes(value).decode()
        for name, binary in (("go", GO), ("rust", RUST)):
            proc = run(binary, value)
            if proc.returncode != 0 or proc.stdout.strip() != py:
                mismatches.append({"i": i, "lang": name, "value": value, "py": py, "out": proc.stdout, "err": proc.stderr})
    runkey_mismatches = []
    for i in range(40):
        material = {key: f"{rng.getrandbits(256):064x}" for key in RUN_KEY_OBJECT_KEYS}
        expected = run_key(material)
        for name, binary in (("go", GO), ("rust", RUST)):
            proc = runkey(binary, material)
            if proc.returncode != 0 or proc.stdout.strip() != expected:
                runkey_mismatches.append({"i": i, "lang": name, "out": proc.stdout, "err": proc.stderr})
        extra = {**material, "attempt_id": "a" * 64}
        if runkey(GO, extra).returncode == 0 or runkey(RUST, extra).returncode == 0:
            runkey_mismatches.append({"i": i, "attempt_id": "accepted"})
    rejects = 0
    for bad in ["e\u0301", "\ud800"]:
        try:
            canonical_bytes(bad)
        except (CanonicalError, UnicodeEncodeError):
            rejects += 1
    out = {
        "vectors": 120,
        "languages": ["python", "go", "rust"],
        "mismatches": mismatches,
        "runkey_vectors": 40,
        "runkey_mismatches": runkey_mismatches,
        "nfc_negative_rejections": rejects,
        "status": "PASS" if not mismatches and not runkey_mismatches and rejects == 2 else "FAIL",
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0 if out["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
