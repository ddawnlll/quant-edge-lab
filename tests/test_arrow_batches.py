"""P0 Arrow reader guards; no claims about native replay parity yet."""

from __future__ import annotations

import hashlib

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from qel.prepare.arrow_batches import ArrowPartitionError, iter_verified_pit_batches


SCHEMA = pa.schema(
    [
        ("event_type", pa.string()),
        ("ts_ns", pa.uint64()),
        ("recv_ns", pa.uint64()),
        ("raw_offset", pa.int64()),
        ("line_sha256", pa.string()),
        ("raw_hash_scope", pa.string()),
        ("raw_hash_verified", pa.bool_()),
        ("source_id", pa.string()),
        ("symbol", pa.string()),
    ]
)


def partition(tmp_path, *, events=None, times=None, offsets=None, schema=SCHEMA):
    events = events if events is not None else ["Quote", "Trade", "Mark", "Funding"]
    times = times if times is not None else [100, 100, 100, 100]
    offsets = offsets if offsets is not None else list(range(len(events)))
    n = len(events)
    cols = {
        "event_type": events,
        "ts_ns": times,
        "recv_ns": [200 + i for i in range(n)],
        "raw_offset": offsets,
        "line_sha256": ["0" * 64] * n,
        "raw_hash_scope": ["RECORD_CANONICAL"] * n,
        "raw_hash_verified": [True] * n,
        "source_id": ["BINANCE_USDM_PUBLIC"] * n,
        "symbol": ["BTCUSDT"] * n,
    }
    cols = {name: cols[name] for name in schema.names}
    path = tmp_path / "events.parquet"
    pq.write_table(pa.Table.from_pydict(cols, schema=schema), path, row_group_size=2)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def read(path, digest, *, rows=2, ordered=True):
    return list(
        iter_verified_pit_batches(
            path,
            expected_sha256=digest,
            batch_rows=rows,
            require_timestamp_order=ordered,
        )
    )


def test_bounded_batches_preserve_same_timestamp_source_order(tmp_path):
    path, digest = partition(tmp_path)
    batches = read(path, digest)
    assert [b.num_rows for b in batches] == [2, 2]
    assert [
        x for batch in batches for x in batch.column(0).to_pylist()
    ] == ["Quote", "Trade", "Mark", "Funding"]
    assert all(isinstance(batch, pa.RecordBatch) for batch in batches)


def test_timestamp_regression_across_batches_fails_closed(tmp_path):
    path, digest = partition(tmp_path, times=[1, 1, 0, 3])
    with pytest.raises(ArrowPartitionError, match="PIT_TIMESTAMP_REGRESSION"):
        read(path, digest, rows=2)
    assert len(read(path, digest, rows=2, ordered=False)) == 2


def test_timestamp_regression_within_batch_fails_closed(tmp_path):
    path, digest = partition(tmp_path, times=[1, 3, 2, 4])
    with pytest.raises(ArrowPartitionError, match="PIT_TIMESTAMP_REGRESSION"):
        read(path, digest, rows=4)


def test_unknown_kind_fails_closed(tmp_path):
    path, digest = partition(tmp_path, events=["Quote", "Trade", "Liquidation", "Funding"])
    with pytest.raises(ArrowPartitionError, match="PIT_UNKNOWN_EVENT"):
        read(path, digest)


def test_null_timestamp_fails_closed(tmp_path):
    path, digest = partition(tmp_path, times=[1, 2, None, 4])
    with pytest.raises(ArrowPartitionError, match="PIT_REQUIRED_NULL"):
        read(path, digest)


def test_missing_provenance_column_fails_closed(tmp_path):
    schema = pa.schema([f for f in SCHEMA if f.name != "line_sha256"])
    path, digest = partition(tmp_path, schema=schema)
    with pytest.raises(ArrowPartitionError, match="PIT_SCHEMA_MISMATCH:line_sha256"):
        read(path, digest)


def test_modified_partition_hash_fails_closed(tmp_path):
    path, digest = partition(tmp_path)
    path.write_bytes(path.read_bytes() + b"tamper")
    with pytest.raises(ArrowPartitionError, match="PARTITION_HASH_MISMATCH"):
        read(path, digest)


@pytest.mark.parametrize("rows", [0, -1, 1_000_001])
def test_invalid_batch_rows_fails_closed(tmp_path, rows):
    path, digest = partition(tmp_path)
    with pytest.raises(ArrowPartitionError, match="INVALID_BATCH_ROWS"):
        read(path, digest, rows=rows)


def test_invalid_expected_hash_fails_closed(tmp_path):
    path, _ = partition(tmp_path)
    with pytest.raises(ArrowPartitionError, match="INVALID_EXPECTED_SHA256"):
        read(path, "not-a-hash")


def test_missing_partition_fails_closed(tmp_path):
    with pytest.raises(ArrowPartitionError, match="PARTITION_MISSING"):
        read(tmp_path / "missing.parquet", "0" * 64)
