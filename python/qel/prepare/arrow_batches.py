"""Fail-closed, bounded Arrow batch access to already-normalized PIT Parquet.

This is an *additive foundation*, not the M2 replay implementation: it
avoids materializing Parquet into Python rows and refuses unpinned partitions.
The current normalizer intentionally stores exact 8dp decimal coefficients as
strings; a future Arrow-native replay schema must be separately certified.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Iterator

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

EVENT_KINDS = ("Quote", "Trade", "Mark", "Funding")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

# These are the essential columns of qel.prepare.normalize's PIT Parquet.
# Do not silently accept a feature table or an unproven exchange tape.
PIT_COLUMNS = {
    "event_type": pa.string(),
    "ts_ns": pa.uint64(),
    "recv_ns": pa.uint64(),
    "raw_offset": pa.int64(),
    "line_sha256": pa.string(),
    "raw_hash_scope": pa.string(),
    "raw_hash_verified": pa.bool_(),
    "source_id": pa.string(),
    "symbol": pa.string(),
}

# Exact-value columns must be integral coefficients or strings. A float here
# would silently reintroduce binary rounding, which the ADR forbids outright.
FORBIDDEN_FLOAT_TYPES = (pa.float16(), pa.float32(), pa.float64())


class ArrowPartitionError(ValueError):
    """Untrusted, malformed or wrongly scoped PIT Parquet input."""


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iter_verified_pit_batches(
    path: str | Path,
    *,
    expected_sha256: str,
    batch_rows: int = 65536,
    require_timestamp_order: bool = False,
) -> Iterator[pa.RecordBatch]:
    """Stream verified Arrow RecordBatches with no Python row materialization.

    The SHA must come from a previously verified dataset certificate/manifest,
    never from the file being consumed. Each batch carries original source
    ordering; for replay callers must explicitly enable timestamp ordering
    and independently prove canonical tie ordering.

    The normalized PIT schema is not the same as the A3 synthetic-L1 tape
    schema. Do not use this reader as an implicit replay/venue certification.
    """
    file = Path(path)
    if not _SHA256.fullmatch(expected_sha256):
        raise ArrowPartitionError("INVALID_EXPECTED_SHA256")
    if batch_rows <= 0 or batch_rows > 1_000_000:
        raise ArrowPartitionError("INVALID_BATCH_ROWS")
    if not file.is_file():
        raise ArrowPartitionError("PARTITION_MISSING")
    actual = sha256_file(file)
    if actual != expected_sha256:
        raise ArrowPartitionError("PARTITION_HASH_MISMATCH")
    try:
        parquet = pq.ParquetFile(file)
        schema = parquet.schema_arrow
    except (OSError, ValueError, pa.ArrowException) as exc:
        raise ArrowPartitionError("PARTITION_UNREADABLE") from exc

    for name, dtype in PIT_COLUMNS.items():
        if name not in schema.names or not schema.field(name).type.equals(dtype):
            raise ArrowPartitionError(f"PIT_SCHEMA_MISMATCH:{name}")
    for field in schema:
        if field.type in FORBIDDEN_FLOAT_TYPES:
            raise ArrowPartitionError(f"PIT_FLOAT_COLUMN:{field.name}")
    if parquet.metadata.num_rows <= 0:
        raise ArrowPartitionError("PIT_EMPTY")

    last_ts: int | None = None
    last_offset: int | None = None
    count = 0
    valid_kinds = pa.array(EVENT_KINDS, type=pa.string())
    for batch in parquet.iter_batches(batch_size=batch_rows):
        if batch.num_rows == 0:
            continue
        kinds = batch.column(batch.schema.get_field_index("event_type"))
        timestamps = batch.column(batch.schema.get_field_index("ts_ns"))
        offsets = batch.column(batch.schema.get_field_index("raw_offset"))
        receipts = batch.column(batch.schema.get_field_index("recv_ns"))
        if any(
            batch.column(batch.schema.get_field_index(name)).null_count
            for name in PIT_COLUMNS
        ):
            raise ArrowPartitionError("PIT_REQUIRED_NULL")
        if not pc.all(pc.is_in(kinds, value_set=valid_kinds)).as_py():
            raise ArrowPartitionError("PIT_UNKNOWN_EVENT")
        # A record cannot be received before the exchange timestamp that
        # describes it; catching this here keeps a swapped column pair out of
        # every downstream replay.
        if not pc.all(pc.greater_equal(receipts, timestamps)).as_py():
            raise ArrowPartitionError("PIT_RECEIPT_BEFORE_EVENT")
        # Source ordinals order the canonical tape; a regression means the
        # partition is not the tape it claims to be.
        if last_offset is not None and offsets[0].as_py() < last_offset:
            raise ArrowPartitionError("PIT_OFFSET_REGRESSION")
        if batch.num_rows > 1 and not pc.all(
            pc.greater_equal(offsets.slice(1), offsets.slice(0, batch.num_rows - 1))
        ).as_py():
            raise ArrowPartitionError("PIT_OFFSET_REGRESSION")
        last_offset = offsets[batch.num_rows - 1].as_py()
        raw_verified = batch.column(batch.schema.get_field_index("raw_hash_verified"))
        if not pc.all(raw_verified).as_py():
            # A partition hash alone cannot certify unverifiable upstream
            # response hashes (normalizer labels such records RESTRICTED).
            raise ArrowPartitionError("PIT_UNVERIFIED_RAW_RECORD")
        if require_timestamp_order:
            first = timestamps[0].as_py()
            if last_ts is not None and first < last_ts:
                raise ArrowPartitionError("PIT_TIMESTAMP_REGRESSION")
            if batch.num_rows > 1 and not pc.all(
                pc.greater_equal(
                    timestamps.slice(1),
                    timestamps.slice(0, batch.num_rows - 1),
                )
            ).as_py():
                raise ArrowPartitionError("PIT_TIMESTAMP_REGRESSION")
            last_ts = timestamps[batch.num_rows - 1].as_py()
        count += batch.num_rows
        yield batch
    if count != parquet.metadata.num_rows:
        raise ArrowPartitionError("PIT_ROW_COUNT_MISMATCH")
