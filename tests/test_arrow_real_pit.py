"""P1b: fresh PIT partition via current normalizer + real Arrow reader test.

The capture here is a small synthesized raw segment (labelled as such in
the dataset certificate via source_id); it is NOT observed market data.
The point is only to prove the PR47 Arrow reader accepts a partition
produced by the current normalizer (with provenance columns), unlike the
legacy checked-in partitions which predate those columns.
"""

from __future__ import annotations

import hashlib
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from qel.contract.canonical import canonical_bytes
from qel.prepare.arrow_batches import iter_verified_pit_batches
from qel.prepare.normalize import normalize_capture


def _record(kind, ts, payload, recv=None):
    return {
        "record_type": kind,
        "source_ts_ns": str(ts),
        "ts_recv_ns": str(recv if recv is not None else ts + 1),
        "source_id": "BINANCE_USDM_PUBLIC",
        "symbol": "BTCUSDT",
        "payload": payload,
    }


def _write_capture(tmp_path, records):
    lines, cursor = [], 0
    for record in records:
        rec = dict(record)
        rec["raw_offset"] = str(cursor)
        body = {k: v for k, v in rec.items() if k != "raw_sha256"}
        rec["raw_sha256"] = hashlib.sha256(canonical_bytes(body)).hexdigest()
        line = json.dumps(rec, sort_keys=True).encode()
        lines.append(line)
        cursor += len(line) + 1
    data = b"\n".join(lines) + b"\n"
    events = tmp_path / "events.jsonl"
    events.write_bytes(data)
    manifest = {
        "schema_version": "1.1.0",
        "segments": [{"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": str(len(data))}],
        "loss_events": [],
    }
    mp = tmp_path / "manifest.json"
    mp.write_text(json.dumps(manifest))
    return events, mp


def test_fresh_pit_partition_readable_by_arrow_reader(tmp_path):
    records = [
        _record("Quote", 1000, {"bidPrice": "100.0", "askPrice": "101.0", "bidQty": "2", "askQty": "3", "lastUpdateId": 1}),
        _record("Trade", 1000, {"p": "100.5", "q": "1.5", "a": 1, "m": False}),
    ]
    events, manifest_path = _write_capture(tmp_path, records)
    result = normalize_capture(str(events), str(manifest_path), str(tmp_path / "out"), "DS-PERF-P1B")
    partition = tmp_path / "out" / "DS-PERF-P1B" / "events.parquet"
    assert partition.is_file()
    digest = hashlib.sha256(partition.read_bytes()).hexdigest()
    assert digest == result["dataset_hash"]
    batches = list(
        iter_verified_pit_batches(partition, expected_sha256=digest, batch_rows=1, require_timestamp_order=True)
    )
    assert sum(b.num_rows for b in batches) == 2
    assert all(isinstance(b, pa.RecordBatch) for b in batches)
    schema = pq.ParquetFile(partition).schema_arrow
    for col in ("line_sha256", "raw_hash_scope", "raw_hash_verified", "raw_len"):
        assert col in schema.names, f"fresh partition missing provenance column {col}"
