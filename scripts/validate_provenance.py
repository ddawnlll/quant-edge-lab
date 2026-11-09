#!/usr/bin/env python3
"""Validate raw-to-normalized provenance and archive/native dataset hashes."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def validate_capture(capture_json: str, dataset_dir: str) -> dict:
    cap = json.loads((ROOT / capture_json).read_text())
    manifest = json.loads((ROOT / cap["manifest_path"]).read_text())
    events = ROOT / cap["events_path"]
    event_hash = sha(events)
    segment = manifest["segments"][0]
    assert segment["sha256"] == event_hash, "raw segment hash mismatch"
    assert int(segment["size_bytes"]) == events.stat().st_size, "raw segment size mismatch"

    records: dict[str, dict] = {}
    offset = 0
    with events.open() as f:
        for line in f:
            rec = json.loads(line)
            records[rec["raw_offset"]] = rec
            offset += len(line.encode())
    assert offset == events.stat().st_size, "offset accounting mismatch"

    table = pq.read_table(ROOT / dataset_dir / "events.parquet").to_pylist()
    resolved = 0
    hash_matches = 0
    normalized_offsets = set()
    for row in table:
        normalized_offsets.add(str(row["raw_offset"]))
        rec = records.get(str(row["raw_offset"]))
        if rec is None:
            continue
        resolved += 1
        if rec.get("raw_sha256") == row["raw_sha256"]:
            hash_matches += 1
    raw_offsets = {str(offset) for offset in records}
    dataset = json.loads((ROOT / dataset_dir / "dataset.json").read_text())
    part = dataset["partitions"][0]
    actual_part_hash = sha(ROOT / dataset_dir / "events.parquet")
    quality = json.loads((ROOT / dataset_dir / "quality.json").read_text())
    quality_ref = dataset["quality_certificate"]
    expected_gaps = [
        {"start_ns": str(event["start_ns"]), "end_ns": str(event["end_ns"])}
        for event in manifest.get("loss_events", [])
    ]
    checks = {
        "raw_segment_hash_ok": True,
        "all_raw_offsets_used": normalized_offsets == raw_offsets,
        "normalized_offsets_unique": len(normalized_offsets) == len(table),
        "row_counts_agree": len(table) == len(records) == int(quality["row_count"]) == int(dataset.get("row_count", len(table))),
        "partition_size_ok": part["size_bytes"] == str((ROOT / dataset_dir / "events.parquet").stat().st_size),
        "dataset_partition_hash_ok": part["sha256"] == actual_part_hash,
        "quality_ref_hash_ok": quality_ref["sha256"] == sha(ROOT / dataset_dir / "quality.json"),
        "quality_dataset_hash_ok": quality["dataset_hash"] == actual_part_hash,
        "quality_clean": quality["duplicates"] == 0 and quality["conflicts"] == 0,
        "gaps_reported": quality["gap_ranges"] == expected_gaps,
        "time_range_agrees": dataset["time_range"] == quality["availability_range"],
    }
    return {
        "capture": cap["capture_id"],
        "raw_events": len(records),
        "normalized_rows": len(table),
        "rows_with_resolved_raw_offset": resolved,
        "rows_with_matching_raw_hash": hash_matches,
        "checks": checks,
        "status": "PASS"
        if resolved == len(table) and hash_matches == len(table) and all(checks.values())
        else "FAIL",
    }


def validate_archive() -> dict:
    manifest = json.loads((ROOT / "evidence/labd/ARCHIVE_NATIVE_MANIFEST.json").read_text())
    native = ROOT / manifest["native_input"]
    counts = manifest["counts"]
    native_rows = sum(1 for _ in native.open())
    checks = {
        "native_sha_ok": sha(native) == manifest["native_sha256"],
        "native_row_count_ok": native_rows == sum(int(v) for v in counts.values()),
        "agg_sha_ok": sha(ROOT / "data/raw/ARCHIVE_BINANCE_USDM/BTCUSDT/2026-11-07/BTCUSDT-aggTrades-2026-11-07.zip")
        == manifest["input_hashes"]["agg_trades"],
        "mark_sha_ok": sha(ROOT / "data/raw/ARCHIVE_BINANCE_USDM/BTCUSDT/2026-11-07/BTCUSDT-1m-2026-11-07.zip")
        == manifest["input_hashes"]["mark_klines"],
        "funding_sha_ok": sha(ROOT / "data/raw/ARCHIVE_BINANCE_USDM/BTCUSDT/2026-11-07/funding.json")
        == manifest["input_hashes"]["funding"],
    }
    return {
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "counts": counts,
        "native_rows": native_rows,
    }


def main() -> int:
    live = validate_capture(
        "evidence/labd/LIVE_CAPTURE_240.json",
        "data/normalized/DS-BTCUSDT-LIVE-240S",
    )
    archive = validate_archive()
    out = {"live_capture": live, "archive_native": archive}
    print(json.dumps(out, indent=2))
    return 0 if live["status"] == "PASS" and archive["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
