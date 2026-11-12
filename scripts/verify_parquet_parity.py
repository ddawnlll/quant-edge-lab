#!/usr/bin/env python3
"""Verify Rust Parquet output against the canonical legacy NDJSON tape.

Usage:
  PYTHONPATH=python .venv/bin/python scripts/verify_parquet_parity.py \
    --parquet <file.parquet> --ndjson <legacy.jsonl> [--out <report.json>]

Checks, using the Python reference decoder from PR #49
(`qel.prepare.archive_parquet.iter_canonical_ndjson_lines`, retained as
an independent reference only):
  * canonical NDJSON sha256 decoded from Parquet == sha256 of legacy file
  * event counts by kind
  * first divergence (line index, both records) if parity fails

This script is a verification tool, not the production replay path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from qel.prepare.archive_parquet import iter_canonical_ndjson_lines


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--parquet", required=True)
    ap.add_argument("--ndjson", required=True)
    ap.add_argument("--out")
    args = ap.parse_args()

    parquet = Path(args.parquet)
    ndjson = Path(args.ndjson)
    legacy_sha = sha256_file(ndjson)
    legacy_lines = ndjson.read_bytes().splitlines()

    decoded_lines = list(iter_canonical_ndjson_lines(parquet))
    h = hashlib.sha256()
    for line in decoded_lines:
        h.update(line)
        h.update(b"\n")
    decoded_sha = h.hexdigest()

    counts: dict[str, int] = {}
    for line in decoded_lines:
        kind = json.loads(line)["kind"]
        counts[kind] = counts.get(kind, 0) + 1

    first_divergence = None
    if decoded_lines != legacy_lines:
        n = min(len(decoded_lines), len(legacy_lines))
        idx = next((i for i in range(n) if decoded_lines[i] != legacy_lines[i]), n)
        first_divergence = {
            "index": idx,
            "parquet_decoded": decoded_lines[idx].decode() if idx < len(decoded_lines) else None,
            "legacy": legacy_lines[idx].decode() if idx < len(legacy_lines) else None,
        }

    report = {
        "parquet": str(parquet),
        "ndjson": str(ndjson),
        "legacy_sha256": legacy_sha,
        "decoded_sha256": decoded_sha,
        "sha_match": legacy_sha == decoded_sha,
        "rows_decoded": len(decoded_lines),
        "rows_legacy": len(legacy_lines),
        "counts_decoded": counts,
        "first_divergence": first_divergence,
        "verdict": "PASS" if legacy_sha == decoded_sha else "FAIL",
    }
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.out:
        Path(args.out).write_text(text)
    print(text)
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
