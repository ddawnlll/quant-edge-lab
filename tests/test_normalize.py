import hashlib
import json
from pathlib import Path

import pytest

from qel.contract.canonical import canonical_bytes
from qel.prepare.normalize import normalize_capture


def _record(kind, ts, payload, recv=None, symbol="BTCUSDT", **extra):
    base = {
        "record_type": kind,
        "source_ts_ns": str(ts),
        "ts_recv_ns": str(recv if recv is not None else ts + 1),
        "source_id": "BINANCE_USDM_PUBLIC",
        "symbol": symbol,
        "payload": payload,
    }
    base.update(extra)
    return base


def quote(ts=1000, recv=1001, bid="100.0", ask="101.0", bid_qty="2", ask_qty="3", uid=None, **extra):
    payload = {"bidPrice": bid, "askPrice": ask, "bidQty": bid_qty, "askQty": ask_qty}
    if uid is not None:
        payload["lastUpdateId"] = uid
    return _record("Quote", ts, payload, recv, **extra)


def trade(ts=1000, recv=1001, price="100.5", qty="1.5", trade_id=1, maker=False, **extra):
    return _record(
        "Trade",
        ts,
        {"p": price, "q": qty, "a": trade_id, "m": maker},
        recv,
        **extra,
    )


def mark(ts=1000, recv=1001, price="100.0", **extra):
    return _record("Mark", ts, {"markPrice": price}, recv, **extra)


def funding(ts=1000, recv=1001, rate="0.00010000", **extra):
    return _record("Funding", ts, {"fundingRate": rate, "fundingTime": ts}, recv, **extra)


def write_capture(
    tmp_path,
    records,
    *,
    raw_format=None,
    raw_hash_scope=None,
    loss_events=None,
    base_offset=None,
    declared_hash=None,
    tamper=None,
    truncate=False,
    blank_line=False,
    manifest_extra=None,
):
    """Write records whose RECORD_CANONICAL raw_sha256 is valid by construction."""
    lines = []
    cursor = 0
    for record in records:
        rec = dict(record)
        rec["raw_offset"] = str(cursor)
        body = {key: value for key, value in rec.items() if key != "raw_sha256"}
        rec["raw_sha256"] = declared_hash or hashlib.sha256(canonical_bytes(body)).hexdigest()
        line = json.dumps(rec, sort_keys=True).encode()
        lines.append(line)
        cursor += len(line) + 1
    data = b"\n".join(lines) + b"\n"
    if tamper is not None:
        old, new = tamper
        assert old in data
        data = data.replace(old, new, 1)
    if truncate:
        data = data[:-1]
    if blank_line:
        data = data.replace(b"\n", b"\n\n", 1)

    events = tmp_path / "events.jsonl"
    events.write_bytes(data)
    manifest = {
        "schema_version": "1.1.0",
        "segments": [
            {
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": str(len(data)),
                "schema_version": "1.1.0",
            }
        ],
        "loss_events": loss_events or [],
    }
    if raw_format is not None:
        manifest["raw_format"] = raw_format
    if raw_hash_scope is not None:
        manifest["raw_hash_scope"] = raw_hash_scope
    if base_offset is not None:
        manifest["raw_offset_base"] = str(base_offset)
    if manifest_extra:
        manifest.update(manifest_extra)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    return events, manifest_path


def read_json(path):
    return json.loads(Path(path).read_text())


def test_normalize_positive_and_deterministic(tmp_path):
    events, manifest = write_capture(
        tmp_path,
        [
            quote(uid=10),
            trade(trade_id=1),
            mark(),
            funding(),
        ],
    )
    first = normalize_capture(str(events), str(manifest), str(tmp_path / "out1"), "DS-TEST")
    assert first["rows"] == 4
    assert first["duplicates"] == 0
    assert first["coverage"] == ["FUNDING", "MARK", "QUOTE", "TRADE"]
    assert first["raw_hash_scope"] == "RECORD_CANONICAL"
    assert first["raw_hash_verified_rows"] == 4
    assert first["raw_hash_unverifiable_rows"] == 0
    assert first["dataset_hash"] == first["dataset_hash"].lower()

    certificate = read_json(first["certificate_path"])
    assert certificate["dataset_manifest"]["partitions"][0]["sha256"] == first["dataset_hash"]
    assert certificate["quality_certificate"]["dataset_hash"] == first["dataset_hash"]
    assert certificate["raw_provenance"]["raw_offset_base"] == "0"
    assert certificate["raw_provenance"]["row_count"] == "4"
    assert certificate["raw_provenance"]["rows_with_line_sha256"] == "4"
    assert certificate["gates"][0]["code"] == "UNIVERSE_UNVERIFIED"
    assert certificate["universe"]["universe_verified"] is False
    assert certificate["path_decision"]["primary_backend"] == "PYARROW_PARQUET"

    quality = read_json(first["quality_path"])
    assert quality["availability_range"] == quality["receive_range"]
    assert quality["row_count"] == "4"
    assert len(quality["snapshot_anchors"]) == 1
    assert quality["snapshot_anchors"][0]["last_update_id"] == "10"

    second = normalize_capture(str(events), str(manifest), str(tmp_path / "out2"), "DS-TEST")
    assert second["dataset_hash"] == first["dataset_hash"]
    assert second["certificate_sha256"] == first["certificate_sha256"]


def test_identical_duplicates_dropped_deterministically(tmp_path):
    events, manifest = write_capture(
        tmp_path,
        [trade(trade_id=7), trade(trade_id=7), quote(uid=3)],
    )
    result = normalize_capture(str(events), str(manifest), str(tmp_path / "out"), "DS-DUP")
    assert result["rows"] == 2
    assert result["duplicates"] == 1
    certificate = read_json(result["certificate_path"])
    assert certificate["duplicates"]["identical_dropped"] == 1
    assert certificate["duplicates"]["by_event_type"]["Trade"] == 1


def test_tampered_raw_byte_rejected(tmp_path):
    events, manifest = write_capture(tmp_path, [quote(bid="100.0", ask="101.0")])
    # Keep the segment checksum consistent with the tampered file so that only
    # per-record recomputation can catch the corruption.
    data = events.read_bytes().replace(b'"100.0"', b'"100.1"', 1)
    events.write_bytes(data)
    raw_manifest = read_json(manifest)
    raw_manifest["segments"][0]["sha256"] = hashlib.sha256(data).hexdigest()
    raw_manifest["segments"][0]["size_bytes"] = str(len(data))
    manifest.write_text(json.dumps(raw_manifest))
    with pytest.raises(ValueError, match="RAW_HASH_MISMATCH"):
        normalize_capture(str(events), str(manifest), str(tmp_path / "out"), "DS-TAMPER")


def test_conflicting_trade_duplicate_rejected(tmp_path):
    events, manifest = write_capture(tmp_path, [trade(trade_id=5, qty="1.0"), trade(trade_id=5, qty="2.0")])
    with pytest.raises(ValueError, match="DUPLICATE_CONFLICT"):
        normalize_capture(str(events), str(manifest), str(tmp_path / "out"), "DS-CONFLICT")


def test_conflicting_quote_duplicate_rejected(tmp_path):
    events, manifest = write_capture(
        tmp_path,
        [quote(uid=9, bid="100.0"), quote(uid=9, bid="100.5")],
    )
    with pytest.raises(ValueError, match="DUPLICATE_CONFLICT"):
        normalize_capture(str(events), str(manifest), str(tmp_path / "out"), "DS-CONFLICT")


@pytest.mark.parametrize(
    "bad_quote, code",
    [
        (quote(bid="102.0", ask="101.0"), "QUOTE_BID_GT_ASK"),
        (quote(bid="0", ask="101.0"), "QUOTE_BID_NONPOSITIVE"),
        (quote(bid="100.0", ask="0"), "QUOTE_ASK_NONPOSITIVE"),
        (quote(bid_qty="-1"), "QUOTE_NEGATIVE_SIZE"),
        (quote(ask_qty="-2"), "QUOTE_NEGATIVE_SIZE"),
    ],
)
def test_invalid_atomic_quote_rejected(tmp_path, bad_quote, code):
    events, manifest = write_capture(tmp_path, [bad_quote])
    with pytest.raises(ValueError, match=code):
        normalize_capture(str(events), str(manifest), str(tmp_path / "out"), "DS-QUOTE")


def test_incomplete_quote_rejected(tmp_path):
    record = _record(
        "Quote",
        1000,
        {"bidPrice": "100", "askPrice": "101", "bidQty": "1"},
    )
    events, manifest = write_capture(tmp_path, [record])
    with pytest.raises(ValueError, match="QUOTE_INCOMPLETE"):
        normalize_capture(str(events), str(manifest), str(tmp_path / "out"), "DS-QUOTE")


@pytest.mark.parametrize("bad_ts", ["1.5", "-1", 1.5, "1000ms", True])
def test_non_integer_timestamp_rejected(tmp_path, bad_ts):
    record = quote()
    record["source_ts_ns"] = bad_ts
    events, manifest = write_capture(tmp_path, [record])
    with pytest.raises(ValueError, match="INVALID_TIMESTAMP"):
        normalize_capture(str(events), str(manifest), str(tmp_path / "out"), "DS-TS")


def test_sequence_regression_rejected(tmp_path):
    events, manifest = write_capture(
        tmp_path,
        [trade(trade_id=1, sequence=2), trade(trade_id=2, sequence=1)],
    )
    with pytest.raises(ValueError, match="SEQUENCE_REGRESSION"):
        normalize_capture(str(events), str(manifest), str(tmp_path / "out"), "DS-SEQ")


def test_sequence_conflict_rejected(tmp_path):
    events, manifest = write_capture(
        tmp_path,
        [trade(trade_id=1, sequence=1), trade(trade_id=2, sequence=1)],
    )
    with pytest.raises(ValueError, match="SEQUENCE_CONFLICT"):
        normalize_capture(str(events), str(manifest), str(tmp_path / "out"), "DS-SEQ")


def test_snapshot_requires_sequence(tmp_path):
    record = trade(trade_id=1, snapshot=True)
    events, manifest = write_capture(tmp_path, [record])
    with pytest.raises(ValueError, match="SNAPSHOT_MISSING_SEQUENCE"):
        normalize_capture(str(events), str(manifest), str(tmp_path / "out"), "DS-SNAP")


def test_snapshot_flag_must_be_boolean(tmp_path):
    record = trade(trade_id=1, snapshot="yes", sequence=1)
    events, manifest = write_capture(tmp_path, [record])
    with pytest.raises(ValueError, match="SNAPSHOT_INVALID"):
        normalize_capture(str(events), str(manifest), str(tmp_path / "out"), "DS-SNAP")


def test_truncated_tail_rejected(tmp_path):
    events, manifest = write_capture(tmp_path, [trade(trade_id=1)], truncate=True)
    with pytest.raises(ValueError, match="TRUNCATED_TAIL"):
        normalize_capture(str(events), str(manifest), str(tmp_path / "out"), "DS-TAIL")


def test_blank_line_rejected(tmp_path):
    events, manifest = write_capture(tmp_path, [trade(trade_id=1), trade(trade_id=2)], blank_line=True)
    with pytest.raises(ValueError, match="MALFORMED_LINE"):
        normalize_capture(str(events), str(manifest), str(tmp_path / "out"), "DS-BLANK")


def test_invalid_dataset_id_rejected(tmp_path):
    events, manifest = write_capture(tmp_path, [trade(trade_id=1)])
    with pytest.raises(ValueError, match="INVALID_DATASET_ID"):
        normalize_capture(str(events), str(manifest), str(tmp_path / "out"), "../../EVIL")


def test_raw_offset_base_and_declared_offset_are_stored(tmp_path):
    events, manifest = write_capture(tmp_path, [trade(trade_id=1)], base_offset=4096)
    result = normalize_capture(str(events), str(manifest), str(tmp_path / "out"), "DS-BASE")
    certificate = read_json(result["certificate_path"])
    assert certificate["raw_provenance"]["raw_offset_base"] == "4096"
    import pyarrow.parquet as pq

    table = pq.read_table(result["partition_path"])
    assert table.column("raw_offset").to_pylist() == [4096]


def test_declared_offset_mismatch_rejected(tmp_path):
    events, manifest = write_capture(tmp_path, [trade(trade_id=1)])
    data = events.read_bytes().replace(b'"raw_offset": "0"', b'"raw_offset": "7"', 1)
    events.write_bytes(data)
    raw_manifest = read_json(manifest)
    raw_manifest["segments"][0]["sha256"] = hashlib.sha256(data).hexdigest()
    raw_manifest["segments"][0]["size_bytes"] = str(len(data))
    manifest.write_text(json.dumps(raw_manifest))
    with pytest.raises(ValueError, match="RAW_OFFSET_MISMATCH"):
        normalize_capture(str(events), str(manifest), str(tmp_path / "out"), "DS-OFFSET")


def test_upstream_hash_is_explicitly_unverifiable(tmp_path):
    events, manifest = write_capture(
        tmp_path,
        [trade(trade_id=1)],
        raw_format="RAW_FRAME_V1",
        declared_hash="0" * 64,
    )
    result = normalize_capture(str(events), str(manifest), str(tmp_path / "out"), "DS-UPSTREAM")
    assert result["raw_hash_scope"] == "UPSTREAM_RESPONSE"
    assert result["raw_hash_unverifiable_rows"] == 1
    certificate = read_json(result["certificate_path"])
    assert "RAW_HASH_UPSTREAM_UNVERIFIABLE" in certificate["restricted_reasons"]
    assert certificate["verdict"] == "RESTRICTED"


def test_payload_canonical_hash_scope_is_strict(tmp_path):
    record = trade(trade_id=1)
    record["raw_offset"] = "0"
    record["raw_sha256"] = hashlib.sha256(canonical_bytes(record["payload"])).hexdigest()
    line = json.dumps(record, sort_keys=True).encode()
    data = line + b"\n"
    events = tmp_path / "events.jsonl"
    events.write_bytes(data)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "segments": [{"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": str(len(data))}],
                "raw_hash_scope": "PAYLOAD_CANONICAL",
                "loss_events": [],
            }
        )
    )
    result = normalize_capture(str(events), str(manifest), str(tmp_path / "out"), "DS-PAYLOAD")
    assert result["raw_hash_scope"] == "PAYLOAD_CANONICAL"
    assert result["raw_hash_verified_rows"] == 1


def test_segment_checksum_alone_does_not_mask_tamper(tmp_path):
    events, manifest = write_capture(tmp_path, [trade(trade_id=1)])
    data = events.read_bytes()
    events.write_bytes(data + b"\n")
    raw_manifest = read_json(manifest)
    raw_manifest["segments"][0]["sha256"] = hashlib.sha256(data + b"\n").hexdigest()
    raw_manifest["segments"][0]["size_bytes"] = str(len(data) + 1)
    manifest.write_text(json.dumps(raw_manifest))
    with pytest.raises(ValueError, match="MALFORMED_LINE"):
        normalize_capture(str(events), str(manifest), str(tmp_path / "out"), "DS-EXTRA")
