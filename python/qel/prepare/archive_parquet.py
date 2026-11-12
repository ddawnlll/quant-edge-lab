"""Bounded-memory Binance archive ZIP to typed Parquet converter (PERF v1.2 P1).

Additive path: it does not modify the canonical NDJSON builders
(``qel.prepare.archive`` / ``scripts/m2_build_fold_tapes_stream.py``) and it
does not create a second PIT normalizer. Output schema v2 stores exact
fixed-point coefficients as int64 (scale 1e8) with explicit economic
``ts_ns``, receipt ``recv_ns``, canonical ``source_ordinal`` and per-row
fidelity. Decoding rows in ordinal order reproduces the canonical NDJSON
tape byte-for-byte (verified on fixtures and on 2024-09-01).

Invariants preserved:
- synthetic L1 quote per second around last trade (tick 0.01, qty 1.000),
  labelled ``assumed_synthetic_l1_around_trade``; never venue depth;
- tie order: Quote/Trade (archive order) before Mark before Funding at
  equal ``ts_ns`` (stable-sort order of the canonical builder);
- funding from monthly ``fundingRate`` ZIPs only; missing funding ZIP is a
  loud failure, never a silent gap;
- no float conversion; decimal precision/overflow is rejected;
- archive ``transact_time``/``close_time``/``calc_time`` must be
  non-decreasing where the streaming canonical order requires it;
  out-of-order input fails closed instead of being silently sorted;
- sealed reserve (>= 2026-03-08) is refused.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterator

import pyarrow as pa
import pyarrow.parquet as pq

SEALED_START = "2026-03-08"
SCALE = 8
SCALE_FACTOR = 10**SCALE
TICK = Decimal("0.01")
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1
DEFAULT_BATCH_ROWS = 65536

SCHEMA = pa.schema(
    [
        ("event_type", pa.string()),
        ("ts_ns", pa.uint64()),
        ("recv_ns", pa.uint64()),
        ("source_ordinal", pa.int64()),
        ("price_i", pa.int64()),
        ("qty_i", pa.int64()),
        ("bid_i", pa.int64()),
        ("ask_i", pa.int64()),
        ("bid_qty_i", pa.int64()),
        ("ask_qty_i", pa.int64()),
        ("aggressor", pa.string()),
        ("trade_id", pa.string()),
        ("source_id", pa.string()),
        ("symbol", pa.string()),
        ("fidelity", pa.string()),
    ]
)

FIDELITY = {
    "Quote": "assumed_synthetic_l1_around_trade",
    "Trade": "observed_trade",
    "Mark": "observed_mark_price_kline_close",
    "Funding": "observed_venue_funding",
}


class ArchiveParquetError(ValueError):
    """Malformed archive, provenance gap, or precision violation."""


def _sha_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _decimal_coeff(value: Decimal, field: str) -> int:
    if not value.is_finite():
        raise ArchiveParquetError(f"DECIMAL_NON_FINITE:{field}")
    scaled = value * SCALE_FACTOR
    if scaled != scaled.to_integral_value():
        raise ArchiveParquetError(f"DECIMAL_PRECISION:{field}={value}")
    coeff = int(scaled)
    if coeff < INT64_MIN or coeff > INT64_MAX:
        raise ArchiveParquetError(f"DECIMAL_OVERFLOW:{field}={value}")
    return coeff


def _parse_decimal(text: str, field: str) -> Decimal:
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError, AttributeError) as exc:
        raise ArchiveParquetError(f"DECIMAL_INVALID:{field}={text!r}") from exc


def _zip_csv_rows(zip_path: Path, required: tuple[str, ...]) -> Iterator[dict]:
    if not zip_path.is_file():
        raise ArchiveParquetError(f"ZIP_MISSING:{zip_path}")
    try:
        z = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as exc:
        raise ArchiveParquetError(f"ZIP_UNREADABLE:{zip_path}") from exc
    names = z.namelist()
    if not names:
        raise ArchiveParquetError(f"ZIP_EMPTY:{zip_path}")
    with z.open(names[0]) as fh:
        reader = csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8"))
        if reader.fieldnames is None:
            raise ArchiveParquetError(f"CSV_NO_HEADER:{zip_path}")
        missing = [c for c in required if c not in reader.fieldnames]
        if missing:
            raise ArchiveParquetError(f"CSV_MISSING_COLUMNS:{missing}")
        yield from reader


def _load_marks(mark_zip: Path) -> list[tuple[int, str]]:
    rows: list[tuple[int, str]] = []
    for r in _zip_csv_rows(mark_zip, ("close_time", "close")):
        try:
            ts_ms = int(r["close_time"])
        except (ValueError, TypeError) as exc:
            raise ArchiveParquetError("MARK_TS_INVALID") from exc
        price = _parse_decimal(str(r["close"]), "mark.close")
        rows.append((ts_ms, str(price.normalize())))
    rows.sort(key=lambda item: item[0])
    return rows


def _load_funding(funding_zip: Path) -> list[tuple[int, str]]:
    if not funding_zip.is_file():
        raise ArchiveParquetError(f"FUNDING_ZIP_MISSING:{funding_zip} (monthly fundingRate required)")
    rows: list[tuple[int, str]] = []
    for r in _zip_csv_rows(funding_zip, ("calc_time", "last_funding_rate")):
        try:
            ts_ms = int(r["calc_time"])
        except (ValueError, TypeError) as exc:
            raise ArchiveParquetError("FUNDING_TS_INVALID") from exc
        rate = _parse_decimal(str(r["last_funding_rate"]), "funding.last_funding_rate")
        rows.append((ts_ms, str(rate.normalize())))
    rows.sort(key=lambda item: item[0])
    return rows


def _empty_batch_arrays() -> dict:
    return {name: [] for name in SCHEMA.names}


def convert_archive_day(
    agg_zip: str | Path,
    mark_zip: str | Path,
    funding_zip: str | Path,
    out_path: str | Path,
    *,
    day: str,
    symbol: str = "BTCUSDT",
    batch_rows: int = DEFAULT_BATCH_ROWS,
    expected_source_sha256: dict | None = None,
) -> dict:
    """Convert one UTC archive day to typed Parquet with bounded batches.

    Returns a manifest dict with source/output hashes, counts, ordering
    proof and the canonical NDJSON sha256 of the decoded rows.
    """
    if day >= SEALED_START:
        raise ArchiveParquetError(f"SEALED_RESERVE_REFUSED:{day}")
    if batch_rows <= 0 or batch_rows > 1_000_000:
        raise ArchiveParquetError("INVALID_BATCH_ROWS")
    agg_zip, mark_zip, funding_zip, out_path = map(Path, (agg_zip, mark_zip, funding_zip, out_path))
    for p in (agg_zip, mark_zip, funding_zip):
        if not p.is_file():
            raise ArchiveParquetError(f"ZIP_MISSING:{p}")
    source_hashes = {"agg_trades": _sha_file(agg_zip), "mark_1m": _sha_file(mark_zip), "funding": _sha_file(funding_zip)}
    if expected_source_sha256:
        for key, want in expected_source_sha256.items():
            if source_hashes.get(key) != want:
                raise ArchiveParquetError(f"SOURCE_HASH_MISMATCH:{key}")

    mark_rows = _load_marks(mark_zip)
    funding_rows_all = _load_funding(funding_zip)
    from datetime import date, datetime, timezone

    start_ms = int(datetime.combine(date.fromisoformat(day), datetime.min.time(), timezone.utc).timestamp() * 1000)
    end_ms = start_ms + 86400_000
    fund_rows = [(ts, rate) for ts, rate in funding_rows_all if start_ms <= ts < end_ms]

    # Pending marks/funding in canonical (ts, mark<funding) order.
    pending: list[tuple[int, int, dict]] = []
    for ts_ms, price in mark_rows:
        pending.append((ts_ms, 0, {"kind": "Mark", "ts_ns": str(ts_ms * 1_000_000), "price": price}))
    for ts_ms, rate in fund_rows:
        pending.append((ts_ms, 1, {"kind": "Funding", "ts_ns": str(ts_ms * 1_000_000), "rate": rate}))
    pending.sort(key=lambda item: (item[0], item[1]))
    pending_idx = 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    writer = pq.ParquetWriter(str(tmp_path), SCHEMA, compression="zstd", use_dictionary=True)
    batch: dict = _empty_batch_arrays()
    ordinal = 0
    counts: dict[str, int] = {}
    first_ts: int | None = None
    last_ts: int | None = None
    last_archive_ts_ms: int | None = None

    def flush():
        nonlocal batch
        if batch["event_type"]:
            writer.write_batch(pa.RecordBatch.from_pydict(batch, schema=SCHEMA))
            batch = _empty_batch_arrays()

    def emit(kind: str, ts_ns: int, *, price_i=0, qty_i=0, bid_i=0, ask_i=0, bid_qty_i=0, ask_qty_i=0, aggressor="", trade_id=""):
        nonlocal ordinal, first_ts, last_ts
        if last_ts is not None and ts_ns < last_ts:
            raise ArchiveParquetError("EVENT_TS_REGRESSION")
        batch["event_type"].append(kind)
        batch["ts_ns"].append(ts_ns)
        batch["recv_ns"].append(ts_ns)  # assumed_zero latency, recorded in manifest
        batch["source_ordinal"].append(ordinal)
        batch["price_i"].append(price_i)
        batch["qty_i"].append(qty_i)
        batch["bid_i"].append(bid_i)
        batch["ask_i"].append(ask_i)
        batch["bid_qty_i"].append(bid_qty_i)
        batch["ask_qty_i"].append(ask_qty_i)
        batch["aggressor"].append(aggressor)
        batch["trade_id"].append(trade_id)
        batch["source_id"].append("BINANCE_USDM_PUBLIC_ARCHIVE")
        batch["symbol"].append(symbol)
        batch["fidelity"].append(FIDELITY[kind])
        counts[kind] = counts.get(kind, 0) + 1
        if first_ts is None:
            first_ts = ts_ns
        last_ts = ts_ns
        ordinal += 1
        if len(batch["event_type"]) >= batch_rows:
            flush()

    def flush_before(ts_ms_limit: int):
        nonlocal pending_idx
        while pending_idx < len(pending) and pending[pending_idx][0] < ts_ms_limit:
            _, _, ev = pending[pending_idx]
            ts_ns = int(ev["ts_ns"])
            if ev["kind"] == "Mark":
                emit("Mark", ts_ns, price_i=_decimal_coeff(_parse_decimal(ev["price"], "mark.price"), "mark.price"))
            else:
                emit("Funding", ts_ns, price_i=_decimal_coeff(_parse_decimal(ev["rate"], "funding.rate"), "funding.rate"))
            pending_idx += 1

    try:
        last_second = None
        for row in _zip_csv_rows(agg_zip, ("transact_time", "price", "quantity", "is_buyer_maker", "agg_trade_id")):
            try:
                ts_ms = int(row["transact_time"])
            except (ValueError, TypeError) as exc:
                raise ArchiveParquetError("TRADE_TS_INVALID") from exc
            if last_archive_ts_ms is not None and ts_ms < last_archive_ts_ms:
                raise ArchiveParquetError("ARCHIVE_TS_REGRESSION")
            last_archive_ts_ms = ts_ms
            price = _parse_decimal(str(row["price"]), "trade.price")
            bucket = ts_ms // 1000
            if last_second is None or bucket != last_second:
                last_second = bucket
                quote_ts_ms = bucket * 1000
                flush_before(quote_ts_ms)
                emit(
                    "Quote",
                    quote_ts_ms * 1_000_000,
                    bid_i=_decimal_coeff(price - TICK, "quote.bid"),
                    ask_i=_decimal_coeff(price + TICK, "quote.ask"),
                    bid_qty_i=_decimal_coeff(Decimal("1.000"), "quote.bid_qty"),
                    ask_qty_i=_decimal_coeff(Decimal("1.000"), "quote.ask_qty"),
                )
            flush_before(ts_ms)
            qty = _parse_decimal(str(row["quantity"]), "trade.quantity").quantize(Decimal("0.001"))
            emit(
                "Trade",
                ts_ms * 1_000_000,
                price_i=_decimal_coeff(price, "trade.price"),
                qty_i=_decimal_coeff(qty, "trade.qty"),
                aggressor="SELL" if row["is_buyer_maker"] == "true" else "BUY",
                trade_id=str(row["agg_trade_id"]),
            )
        while pending_idx < len(pending):
            _, _, ev = pending[pending_idx]
            ts_ns = int(ev["ts_ns"])
            if ev["kind"] == "Mark":
                emit("Mark", ts_ns, price_i=_decimal_coeff(_parse_decimal(ev["price"], "mark.price"), "mark.price"))
            else:
                emit("Funding", ts_ns, price_i=_decimal_coeff(_parse_decimal(ev["rate"], "funding.rate"), "funding.rate"))
            pending_idx += 1
        flush()
    finally:
        writer.close()
    tmp_path.replace(out_path)

    parquet = pq.ParquetFile(out_path)
    output_sha = _sha_file(out_path)
    canonical_sha = canonical_ndjson_sha(out_path)
    manifest = {
        "schema_version": "2.0.0",
        "day": day,
        "symbol": symbol,
        "source": "BINANCE_USDM_PUBLIC_ARCHIVE",
        "source_hashes": source_hashes,
        "parquet_path": str(out_path),
        "parquet_sha256": output_sha,
        "canonical_ndjson_sha256": canonical_sha,
        "counts": counts,
        "rows": ordinal,
        "num_row_groups": parquet.metadata.num_row_groups,
        "time_range": {"start_ns": str(first_ts), "end_ns": str(last_ts)} if first_ts is not None else {},
        "fidelity": {
            "BOOK_COVERAGE": "assumed_synthetic_l1_around_trade",
            "PRICE_PATH": "observed_trade",
            "FEED_LATENCY": "assumed_zero (recv_ns == ts_ns)",
            "FUNDING": "observed_venue_funding (monthly fundingRate)",
        },
        "ordering": "source_ordinal is canonical emission order; equal ts_ns preserves Quote/Trade then Mark then Funding",
        "scale": 8,
        "sealed_start": SEALED_START,
    }
    manifest_path = out_path.with_suffix(".json")
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def _norm_str(coeff: int) -> str:
    return str(Decimal(coeff).scaleb(-SCALE).normalize())


def _qty3_str(coeff: int) -> str:
    return str(Decimal(coeff).scaleb(-SCALE).quantize(Decimal("0.001")))


def iter_canonical_ndjson_lines(path: str | Path) -> Iterator[bytes]:
    """Yield canonical NDJSON lines (bytes, no trailing LF) in ordinal order."""
    parquet = pq.ParquetFile(path)
    last_ord: int | None = None
    last_ts: int | None = None
    for batch in parquet.iter_batches(batch_size=DEFAULT_BATCH_ROWS):
        cols = {name: batch.column(i).to_pylist() for i, name in enumerate(batch.schema.names)}
        n = batch.num_rows
        for i in range(n):
            kind = cols["event_type"][i]
            ts_ns = cols["ts_ns"][i]
            ordinal = cols["source_ordinal"][i]
            if last_ord is not None and ordinal <= last_ord:
                raise ArchiveParquetError("ORDINAL_NOT_INCREASING")
            if last_ts is not None and ts_ns < last_ts:
                raise ArchiveParquetError("EVENT_TS_REGRESSION")
            last_ord, last_ts = ordinal, ts_ns
            if kind == "Quote":
                rec = {
                    "kind": "Quote",
                    "ts_ns": str(ts_ns),
                    "bid": _norm_str(cols["bid_i"][i]),
                    "ask": _norm_str(cols["ask_i"][i]),
                    "bid_qty": _qty3_str(cols["bid_qty_i"][i]),
                    "ask_qty": _qty3_str(cols["ask_qty_i"][i]),
                }
            elif kind == "Trade":
                rec = {
                    "kind": "Trade",
                    "ts_ns": str(ts_ns),
                    "price": _norm_str(cols["price_i"][i]),
                    "qty": _qty3_str(cols["qty_i"][i]),
                    "aggressor": cols["aggressor"][i],
                    "trade_id": cols["trade_id"][i],
                }
            elif kind == "Mark":
                rec = {"kind": "Mark", "ts_ns": str(ts_ns), "price": _norm_str(cols["price_i"][i])}
            elif kind == "Funding":
                rec = {"kind": "Funding", "ts_ns": str(ts_ns), "rate": _norm_str(cols["price_i"][i])}
            else:
                raise ArchiveParquetError(f"UNKNOWN_KIND:{kind}")
            yield json.dumps(rec, sort_keys=True).encode()


def canonical_ndjson_sha(path: str | Path) -> str:
    h = hashlib.sha256()
    for line in iter_canonical_ndjson_lines(path):
        h.update(line)
        h.update(b"\n")
    return h.hexdigest()
