"""Normalize raw public captures into typed Arrow/Parquet datasets."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from ..contract.canonical import hash_jcs, parse_decimal, rescale, sha256_hex
from ..contract.schema import validate_def

PRICE_EXPONENT = 8
QTY_EXPONENT = 8


def _canonical_decimal(text: str) -> str:
    d = Decimal(text)
    if d == 0:
        return "0"
    out = format(d.normalize(), "f")
    if "." in out:
        out = out.rstrip("0").rstrip(".")
    return out


def _coeff(text: str, exponent: int) -> str:
    coeff, scale = parse_decimal(_canonical_decimal(text))
    return str(rescale(coeff, scale, exponent))


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def normalize_capture(events_path: str, manifest_path: str, out_root: str, dataset_id: str | None = None) -> dict:
    events = Path(events_path)
    manifest = json.loads(Path(manifest_path).read_text())
    segment = (manifest.get("segments") or [None])[0]
    if not segment:
        raise ValueError("manifest has no raw segment")
    if segment.get("sha256") != _hash_file(events):
        raise ValueError("raw segment sha256 does not match manifest")
    if segment.get("size_bytes") != str(events.stat().st_size):
        raise ValueError("raw segment size does not match manifest")
    dataset_id = dataset_id or ("DS-" + Path(events_path).stem.upper().replace("-", "_")[:60])
    rows = []
    seen_trades: dict[str, str] = {}
    duplicates = 0
    conflicts = 0
    raw_hashes: set[str] = set()
    start_ns = None
    end_ns = None
    with events.open() as f:
        offset = 0
        for line in f:
            raw = line.encode()
            rec = json.loads(line)
            if rec.get("raw_sha256"):
                raw_hashes.add(rec["raw_sha256"])
            kind = rec["record_type"]
            payload = rec["payload"]
            ts = int(rec["source_ts_ns"])
            start_ns = ts if start_ns is None else min(start_ns, ts)
            end_ns = ts if end_ns is None else max(end_ns, ts)
            row = {
                "event_type": kind,
                "ts_ns": ts,
                "recv_ns": int(rec["ts_recv_ns"]),
                "price_i": None,
                "qty_i": None,
                "bid_i": None,
                "ask_i": None,
                "bid_qty_i": None,
                "ask_qty_i": None,
                "aggressor": None,
                "trade_id": None,
                "raw_sha256": rec["raw_sha256"],
                "raw_offset": offset,
                "source_id": rec["source_id"],
                "symbol": rec["symbol"],
            }
            if kind == "Quote":
                row.update(
                    bid_i=_coeff(payload["bidPrice"], PRICE_EXPONENT),
                    ask_i=_coeff(payload["askPrice"], PRICE_EXPONENT),
                    bid_qty_i=_coeff(payload["bidQty"], QTY_EXPONENT),
                    ask_qty_i=_coeff(payload["askQty"], QTY_EXPONENT),
                )
            elif kind == "Trade":
                tid = str(payload["a"])
                row.update(
                    price_i=_coeff(payload["p"], PRICE_EXPONENT),
                    qty_i=_coeff(payload["q"], QTY_EXPONENT),
                    aggressor="SELL" if payload.get("m") else "BUY",
                    trade_id=tid,
                )
                prior = seen_trades.get(tid)
                if prior is not None:
                    duplicates += 1
                    if prior != json.dumps(payload, sort_keys=True):
                        conflicts += 1
                    continue
                seen_trades[tid] = json.dumps(payload, sort_keys=True)
            elif kind == "Mark":
                row.update(price_i=_coeff(payload["markPrice"], PRICE_EXPONENT))
            elif kind == "Funding":
                row.update(price_i=_coeff(payload["fundingRate"], 8))
            rows.append(row)
            offset += len(raw)

    table = pa.Table.from_pylist(
        rows,
        schema=pa.schema(
            [
                ("event_type", pa.string()),
                ("ts_ns", pa.uint64()),
                ("recv_ns", pa.uint64()),
                ("price_i", pa.string()),
                ("qty_i", pa.string()),
                ("bid_i", pa.string()),
                ("ask_i", pa.string()),
                ("bid_qty_i", pa.string()),
                ("ask_qty_i", pa.string()),
                ("aggressor", pa.string()),
                ("trade_id", pa.string()),
                ("raw_sha256", pa.string()),
                ("raw_offset", pa.int64()),
                ("source_id", pa.string()),
                ("symbol", pa.string()),
            ]
        ),
    )
    out = Path(out_root) / dataset_id
    out.mkdir(parents=True, exist_ok=True)
    partition = out / "events.parquet"
    pq.write_table(table, partition, compression="zstd", row_group_size=65536)
    dataset_hash = _hash_file(partition)
    schema_version = "1.1.0"
    license_id = manifest.get("license_id", "BINANCE_PUBLIC_DATA_TERMS")
    coverage = sorted({r["event_type"].upper() for r in rows})
    quality = {
        "dataset_hash": dataset_hash,
        "normalizer_hash": sha256_hex(Path(__file__).read_bytes()),
        "raw_hashes": sorted(raw_hashes),
        "partition_count": 1,
        "row_count": str(len(rows)),
        "atomic_message_count": str(len(rows)),
        "exchange_range": {"start_ns": str(start_ns or 0), "end_ns": str(end_ns or 0)},
        "receive_range": {"start_ns": str(min(r["recv_ns"] for r in rows)), "end_ns": str(max(r["recv_ns"] for r in rows))},
        "availability_range": {"start_ns": str(start_ns or 0), "end_ns": str(end_ns or 0)},
        "duplicates": duplicates,
        "conflicts": conflicts,
        "gap_ranges": [],
        "clock_uncertainty_ns": "5000000",
        "coverage": coverage,
        "universe_verified": False,
        "quarantine_hashes": [],
        "snapshot_anchors": [],
        "verdict": "RESTRICTED",
    }
    validate_def("QualityCertificate", quality)
    qpath = out / "quality.json"
    qpath.write_text(json.dumps(quality, indent=2, sort_keys=True) + "\n")
    partition_ref = {
        "sha256": dataset_hash,
        "schema_id": "urn:qel:dataset:events",
        "schema_version": schema_version,
        "size_bytes": str(partition.stat().st_size),
        "visibility": "DEVELOPMENT",
        "media_type": "application/vnd.apache.parquet",
    }
    quality_ref = {
        "sha256": _hash_file(qpath),
        "schema_id": "urn:qel:dataset:quality",
        "schema_version": schema_version,
        "size_bytes": str(qpath.stat().st_size),
        "visibility": "DEVELOPMENT",
        "media_type": "application/json",
    }
    universe = {
        "schema_version": schema_version,
        "symbols": [{"symbol": rows[0]["symbol"] if rows else "UNKNOWN", "verified_history": False}],
        "note": "single-instrument capture; cross-sectional universe unverified",
    }
    dataset = {
        "schema_version": schema_version,
        "dataset_id": dataset_id,
        "partitions": [partition_ref],
        "quality_certificate": quality_ref,
        "universe_hash": hash_jcs(universe),
        "reference_hash": hash_jcs({"instrument": rows[0]["symbol"] if rows else "UNKNOWN", "price_exponent": PRICE_EXPONENT, "qty_exponent": QTY_EXPONENT}),
        "time_range": {"start_ns": str(start_ns or 0), "end_ns": str(end_ns or 0)},
        "availability_policy_hash": hash_jcs({"known_at": "RECEIVE_ONLY", "no_future_data": True}),
        "license_id": license_id,
        "redistribution": False,
        "revision_of": None,
    }
    validate_def("DatasetManifest", dataset)
    dpath = out / "dataset.json"
    dpath.write_text(json.dumps(dataset, indent=2, sort_keys=True) + "\n")
    return {
        "dataset_id": dataset_id,
        "dataset_path": str(dpath),
        "partition_path": str(partition),
        "rows": len(rows),
        "duplicates": duplicates,
        "conflicts": conflicts,
        "coverage": coverage,
        "dataset_hash": dataset_hash,
        "universe_verified": False,
    }
