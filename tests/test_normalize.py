import hashlib
import json

import pytest

from qel.prepare.normalize import normalize_capture


def capture(tmp_path):
    record = {
        "record_type": "Quote",
        "source_ts_ns": "1000",
        "ts_recv_ns": "1001",
        "raw_sha256": "a" * 64,
        "source_id": "SRC-BINANCE-FUTURES",
        "symbol": "BTCUSDT",
        "payload": {"bidPrice": "100.0", "askPrice": "101.0", "bidQty": "2", "askQty": "3"},
    }
    events = tmp_path / "events.jsonl"
    events.write_text(json.dumps(record) + "\n")
    raw = events.read_bytes()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "size_bytes": str(len(raw)),
                        "schema_version": "1.1.0",
                    }
                ],
                "loss_events": [{"start_ns": "1500", "end_ns": "1600", "reason": "CLOCK_JUMP"}],
            }
        )
    )
    return events, manifest


def test_normalize_and_tamper_rejected(tmp_path):
    events, manifest = capture(tmp_path)
    result = normalize_capture(str(events), str(manifest), str(tmp_path / "out"), "DS-TEST")
    assert result["rows"] == 1
    quality = json.loads((tmp_path / "out/DS-TEST/quality.json").read_text())
    assert quality["gap_ranges"] == [{"start_ns": "1500", "end_ns": "1600"}]
    events.write_text(events.read_text() + "\n")
    with pytest.raises(ValueError, match="segment"):
        normalize_capture(str(events), str(manifest), str(tmp_path / "out2"), "DS-TEST-2")
