import hashlib
import json
from pathlib import Path

import pytest

from qel.contract.canonical import canonical_bytes
from qel.prepare.normalize import (
    BitemporalRuleBook,
    ConflictingRule,
    FutureKnownRule,
    RuleError,
    normalize_capture,
)

KNOWN_END = "1000000000000"
VALID_END = "1000000000000"


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


def quote(ts=1000, recv=1001, uid=1, **extra):
    return _record(
        "Quote",
        ts,
        {"bidPrice": "100.0", "askPrice": "101.0", "bidQty": "2", "askQty": "3", "lastUpdateId": uid},
        recv,
        **extra,
    )


def trade(ts=1000, recv=1001, trade_id=1, **extra):
    return _record("Trade", ts, {"p": "100.5", "q": "1.5", "a": trade_id, "m": False}, recv, **extra)


def write_capture(tmp_path, records, declared_hash=None):
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
    events = tmp_path / "events.jsonl"
    events.write_bytes(data)
    manifest = {
        "schema_version": "1.1.0",
        "segments": [{"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": str(len(data))}],
        "loss_events": [],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    return events, manifest_path


def write_rules(tmp_path, document):
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(document))
    return path


def status_rule(start, end, known_start, known_end=KNOWN_END, status="TRADING", source_hash=None):
    return {
        "instrument_id": "BTCUSDT",
        "status": status,
        "valid_range": {"start_ns": str(start), "end_ns": str(end)},
        "known_range": {"start_ns": str(known_start), "end_ns": str(known_end)},
        "source_hash": source_hash or ("a" * 64),
    }


def instrument_rule(start=0, end=int(VALID_END), known_start=0, status="TRADING"):
    return {
        "instrument_id": "BTCUSDT",
        "venue_id": "BINANCE_USDM",
        "symbol": "BTCUSDT",
        "product": "LINEAR_PERPETUAL",
        "base_currency": "BTC",
        "quote_currency": "USDT",
        "settlement_currency": "USDT",
        "price_exponent": 8,
        "qty_exponent": 8,
        "multiplier": {"num": "1", "den": "1"},
        "tick_i": "1",
        "lot_i": "1",
        "min_qty_i": "1",
        "max_qty_i": "1000000000",
        "min_notional_i": "1",
        "max_leverage": "125",
        "status": status,
        "valid_range": {"start_ns": str(start), "end_ns": str(end)},
        "known_range": {"start_ns": str(known_start), "end_ns": KNOWN_END},
        "source_hash": "b" * 64,
    }


def fee_rule(start=0, end=int(VALID_END), known_start=0):
    return {
        "account_class": "BASIC",
        "maker_rate": {"num": "2", "den": "10000"},
        "taker_rate": {"num": "4", "den": "10000"},
        "currency_id": "USDT",
        "rounding": "HALF_UP",
        "valid_range": {"start_ns": str(start), "end_ns": str(end)},
        "known_range": {"start_ns": str(known_start), "end_ns": KNOWN_END},
        "source_hash": "c" * 64,
    }


def margin_envelope(start=0, end=int(VALID_END), known_start=0):
    return {
        "instrument_id": "BTCUSDT",
        "valid_range": {"start_ns": str(start), "end_ns": str(end)},
        "known_range": {"start_ns": str(known_start), "end_ns": KNOWN_END},
        "source_hash": "d" * 64,
        "tiers": [
            {
                "floor_notional_i": "0",
                "cap_notional_i": "1000000",
                "initial_rate": {"num": "1", "den": "10"},
                "maintenance_rate": {"num": "1", "den": "100"},
                "maintenance_deduction_i": "0",
                "liquidation_fee_rate": {"num": "1", "den": "100"},
                "max_leverage": "10",
            }
        ],
    }


def membership(start=0, end=int(VALID_END), known_start=0, member=True):
    return {
        "universe_id": "U-BTC",
        "venue_id": "BINANCE_USDM",
        "symbol": "BTCUSDT",
        "member": member,
        "valid_range": {"start_ns": str(start), "end_ns": str(end)},
        "known_range": {"start_ns": str(known_start), "end_ns": KNOWN_END},
        "source_hash": "e" * 64,
    }


def full_document():
    return {
        "instrument_rules": [instrument_rule()],
        "fee_rules": [fee_rule()],
        "margin_tiers": [margin_envelope()],
        "trading_status": [status_rule(0, int(VALID_END), 0)],
        "universe_membership": [membership()],
        "universe_snapshot": {
            "universe_id": "U-BTC",
            "complete": True,
            "valid_range": {"start_ns": "0", "end_ns": VALID_END},
            "known_range": {"start_ns": "0", "end_ns": KNOWN_END},
        },
    }


def test_future_known_rule_rejected(tmp_path):
    events, manifest = write_capture(tmp_path, [quote(ts=1000, recv=1001)])
    rules = write_rules(
        tmp_path,
        {"trading_status": [status_rule(0, int(VALID_END), 5000, known_end=6000)]},
    )
    with pytest.raises(FutureKnownRule, match="RULE_FUTURE_KNOWN"):
        normalize_capture(str(events), str(manifest), str(tmp_path / "out"), "DS-PIT", str(rules))


def test_known_rule_accepted(tmp_path):
    events, manifest = write_capture(tmp_path, [quote(ts=1000, recv=1001)])
    rules = write_rules(tmp_path, {"trading_status": [status_rule(0, int(VALID_END), 0)]})
    result = normalize_capture(str(events), str(manifest), str(tmp_path / "out"), "DS-PIT", str(rules))
    assert result["rows"] == 1
    artifact = json.loads((Path(result["certificate_path"]).parent / "rules.json").read_text())
    assert artifact["validation"]["TradingStatus"] == 1


def test_conflicting_rule_at_same_valid_and_known_time_rejected(tmp_path):
    events, manifest = write_capture(tmp_path, [quote(ts=1000, recv=1001)])
    rules = write_rules(
        tmp_path,
        {
            "trading_status": [
                status_rule(0, int(VALID_END), 0, status="TRADING", source_hash="1" * 64),
                status_rule(0, int(VALID_END), 0, status="HALTED", source_hash="2" * 64),
            ]
        },
    )
    with pytest.raises(ConflictingRule, match="RULE_CONFLICT"):
        normalize_capture(str(events), str(manifest), str(tmp_path / "out"), "DS-PIT", str(rules))


def test_historical_listing_and_delisting_preserved():
    document = {
        "trading_status": [
            status_rule(0, 1000, 0, status="TRADING"),
            status_rule(1000, 2000, 0, status="DELISTED"),
        ]
    }
    book = BitemporalRuleBook(document)
    listed = book.resolve("TRADING_STATUS", "BTCUSDT", 999, 999)
    delisted = book.resolve("TRADING_STATUS", "BTCUSDT", 1000, 1000)
    assert listed.payload["status"] == "TRADING"
    assert delisted.payload["status"] == "DELISTED"


def test_boundary_at_valid_to_is_exclusive():
    book = BitemporalRuleBook(
        {
            "trading_status": [
                status_rule(0, 1000, 0, status="TRADING"),
                status_rule(1000, 2000, 0, status="DELISTED"),
            ]
        }
    )
    assert book.resolve("TRADING_STATUS", "BTCUSDT", 0, 0).payload["status"] == "TRADING"
    assert book.resolve("TRADING_STATUS", "BTCUSDT", 1000, 1000).payload["status"] == "DELISTED"


def test_unknown_universe_emits_universe_unverified(tmp_path):
    events, manifest = write_capture(tmp_path, [trade(ts=1000, recv=1001)])
    result = normalize_capture(str(events), str(manifest), str(tmp_path / "out"), "DS-UNIV")
    assert result["universe_verified"] is False
    gate = result["gates"][0]
    assert gate["code"] == "UNIVERSE_UNVERIFIED"
    assert gate["status"] == "BLOCKED"
    assert gate["severity"] == "BLOCKING"
    assert gate["blocks"] == ["CROSS_SECTIONAL_CLAIM", "GENERALIZED_UNIVERSE_CLAIM"]
    certificate = json.loads(Path(result["certificate_path"]).read_text())
    assert certificate["universe"]["universe_verified"] is False
    assert certificate["quality_certificate"]["universe_verified"] is False


def test_complete_universe_evidence_verifies_membership(tmp_path):
    events, manifest = write_capture(tmp_path, [trade(ts=1000, recv=1001)])
    rules = write_rules(tmp_path, full_document())
    result = normalize_capture(str(events), str(manifest), str(tmp_path / "out"), "DS-UNIV", str(rules))
    assert result["universe_verified"] is True
    assert result["gates"][0]["code"] == "UNIVERSE_VERIFIED"
    assert result["gates"][0]["status"] == "PASS"


def test_universe_membership_without_complete_snapshot_is_unverified(tmp_path):
    events, manifest = write_capture(tmp_path, [trade(ts=1000, recv=1001)])
    document = full_document()
    document["universe_snapshot"] = {"universe_id": "U-BTC", "complete": False}
    rules = write_rules(tmp_path, document)
    result = normalize_capture(str(events), str(manifest), str(tmp_path / "out"), "DS-UNIV", str(rules))
    assert result["universe_verified"] is False
    assert result["gates"][0]["reason"] == "NO_COMPLETE_UNIVERSE_SNAPSHOT"


def test_full_rule_bundle_is_schema_validated(tmp_path):
    events, manifest = write_capture(tmp_path, [trade(ts=1000, recv=1001)])
    rules = write_rules(tmp_path, full_document())
    result = normalize_capture(str(events), str(manifest), str(tmp_path / "out"), "DS-RULES", str(rules))
    artifact = json.loads((Path(result["certificate_path"]).parent / "rules.json").read_text())
    assert artifact["validation"] == {
        "InstrumentRule": 1,
        "FeeRule": 1,
        "MarginTier": 1,
        "TradingStatus": 1,
        "UniverseMembership": 1,
    }
    certificate = json.loads(Path(result["certificate_path"]).read_text())
    assert certificate["rules"]["present"] is True
    assert certificate["rules"]["rules_sha256"]


def test_invalid_instrument_rule_schema_rejected(tmp_path):
    events, manifest = write_capture(tmp_path, [trade(ts=1000, recv=1001)])
    document = full_document()
    del document["instrument_rules"][0]["tick_i"]
    rules = write_rules(tmp_path, document)
    with pytest.raises(RuleError, match="RULES_SCHEMA_INVALID"):
        normalize_capture(str(events), str(manifest), str(tmp_path / "out"), "DS-RULES", str(rules))


def test_rule_known_at_boundary_is_inclusive():
    book = BitemporalRuleBook({"trading_status": [status_rule(0, 1000, 500, known_end=600)]})
    # At decision_ns == known_at the rule is knowable; one ns earlier is not.
    assert book.resolve("TRADING_STATUS", "BTCUSDT", 700, 500).payload["status"] == "TRADING"
    with pytest.raises(FutureKnownRule):
        book.resolve("TRADING_STATUS", "BTCUSDT", 700, 499)
