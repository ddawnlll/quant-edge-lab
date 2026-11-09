"""TASK-11 native funding certification tests.

The native taker tape is produced by ``native/runner`` against the pinned
Nautilus build. These tests certify that:

* funding postings are in the primary economic tape (not a side key),
* settlement identity is deterministic and survives distinct equal amounts,
* the independent reconstructor reproduces the native account snapshot exactly
  (``unexplained_residual == 0`` for cash, position and equity),
* removing, repeating or reordering funding is observable or rejected.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from qel.accounting.core import AccountingError, dec, reconstruct

EVIDENCE = Path(__file__).resolve().parents[1] / "evidence" / "labd"
ARCHIVE = EVIDENCE / "NATIVE_FUNDING_RUN.json"
LIVE_240S = EVIDENCE / "NATIVE_FUNDING_RUN_240S.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _audit(tape: list[dict], native: dict) -> tuple[dict, dict]:
    out = reconstruct(tape)
    return out, {
        "residual": dec(out["unexplained_residual"]),
        "cash_diff": dec(out["total_cash"]) - dec(native["total"]),
        "equity_diff": dec(out["equity"]) - dec(native["equity"]),
        "position_diff": dec(out["position_qty"]) - dec(native["position_qty"]),
    }


def _first_funding(tape: list[dict]) -> dict:
    return next(event for event in tape if event["type"] == "FUNDING")


def test_archive_funding_is_in_primary_tape_and_reconciles_exactly():
    doc = _load(ARCHIVE)
    tape = doc["tape"]
    funding = [event for event in tape if event["type"] == "FUNDING"]

    # One authoritative tape: the old side channel is gone.
    assert "tape_extra" not in doc
    assert len(funding) == doc["funding"]["count"] >= 1
    assert doc["funding"]["total"] != "0"
    assert doc["funding"]["amount_currency"] == "USDT"
    assert doc["funding"]["amount_exponent"] == 8

    # Deterministic settlement identity plus funding ownership on every posting.
    settlement_ids = set()
    for event in funding:
        assert event["settlement_id"].startswith("FUND:")
        assert event["settlement_id"] not in settlement_ids
        settlement_ids.add(event["settlement_id"])
        assert event["epoch"]
        assert event["position_id"]
        assert event["account_id"]
        assert event["currency"] == "USDT"
        assert event["ts_ns"]

    # Funding is causally interleaved, not appended after MARK/END_FOLD.
    assert tape[-2]["type"] == "MARK"
    assert tape[-1]["type"] == "END_FOLD"
    for index, event in enumerate(tape):
        if event["type"] == "FUNDING":
            assert index < len(tape) - 2

    out, diffs = _audit(tape, doc["native_account"])
    assert out["unexplained_residual"] == "0"
    assert diffs["residual"] == 0
    assert diffs["cash_diff"] == 0
    assert diffs["equity_diff"] == 0
    assert diffs["position_diff"] == 0
    assert dec(out["funding_cash"]) == dec(doc["funding"]["total"])


def test_live_240s_reconciles_exactly():
    doc = _load(LIVE_240S)
    out, diffs = _audit(doc["tape"], doc["native_account"])
    assert out["unexplained_residual"] == "0"
    assert diffs["cash_diff"] == 0
    assert diffs["equity_diff"] == 0
    assert diffs["position_diff"] == 0


def test_removed_funding_is_visible_as_native_mismatch():
    doc = deepcopy(_load(ARCHIVE))
    native = doc["native_account"]
    funding_total = sum(
        dec(event["amount"]) for event in doc["tape"] if event["type"] == "FUNDING"
    )
    assert funding_total != 0
    doc["tape"] = [event for event in doc["tape"] if event["type"] != "FUNDING"]

    out, diffs = _audit(doc["tape"], native)
    # The internal identity still balances, but the native cash/equity
    # comparison exposes the missing settlement.
    assert out["funding_cash"] == "0"
    assert diffs["cash_diff"] == -funding_total
    assert diffs["equity_diff"] == -funding_total


def test_repeated_settlement_is_rejected():
    doc = deepcopy(_load(ARCHIVE))
    tape = doc["tape"]
    event = _first_funding(tape)
    tape.insert(tape.index(event) + 1, deepcopy(event))
    with pytest.raises(AccountingError, match="duplicate funding settlement"):
        reconstruct(tape)


def test_late_funding_after_mark_is_accepted_and_attributed_to_original_epoch():
    # F09 permits a late receipt: the native tape order is authoritative, the
    # cash belongs to the referenced (older) epoch, and moving the posting later
    # in the tape does not change attribution or the final cash identity.
    doc = deepcopy(_load(ARCHIVE))
    tape = doc["tape"]
    event = tape.pop(next(i for i, e in enumerate(tape) if e["type"] == "FUNDING"))
    epoch = event["epoch"]
    amount = event["amount"]
    mark_index = next(i for i, e in enumerate(tape) if e["type"] == "MARK")
    tape.insert(mark_index + 1, event)
    out = reconstruct(tape)
    assert out["funding_by_epoch"][epoch] == amount
    assert out["unexplained_residual"] == "0"


def test_funding_after_end_fold_is_rejected():
    doc = deepcopy(_load(ARCHIVE))
    tape = doc["tape"]
    tape.append(deepcopy(_first_funding(tape)))
    with pytest.raises(AccountingError, match="after END_FOLD"):
        reconstruct(tape)


def test_f09_late_funding_receipt_is_attributed_to_original_epoch():
    tape = [
        {"type": "SET_WALLET", "cash": "1000"},
        {"type": "SET_POSITION", "qty": "2", "entry": "100", "epoch": "1", "ts_ns": "90"},
        {"type": "FILL", "side": "SELL", "qty": "3", "price": "100", "fee": "0", "ts_ns": "110"},
        # Receipt arrives after the roll to epoch 2 but the economic settlement
        # (ts 100) belongs to epoch 1.
        {"type": "FUNDING", "amount": "-2", "epoch": "1", "settlement_id": "SET-1", "ts_ns": "100"},
        {"type": "MARK", "price": "100", "ts_ns": "200"},
    ]
    out = reconstruct(tape)
    assert out["funding_cash"] == "-2"
    assert out["funding_by_epoch"]["1"] == "-2"
    assert out["funding_by_epoch"].get("2", "0") == "0"
    assert out["funding_settlement_count"] == 1
    assert out["position_qty"] == "-1"
    assert out["unexplained_residual"] == "0"

    duplicate = deepcopy(tape)
    duplicate.append(deepcopy(duplicate[3]))
    with pytest.raises(AccountingError, match="duplicate funding settlement"):
        reconstruct(duplicate)


def test_funding_referencing_a_future_epoch_is_rejected():
    doc = deepcopy(_load(ARCHIVE))
    event = _first_funding(doc["tape"])
    event["epoch"] = str(int(event["epoch"]) + 1000)
    with pytest.raises(AccountingError, match="no open position history"):
        reconstruct(doc["tape"])


def test_funding_ts_outside_referenced_epoch_window_is_rejected():
    doc = deepcopy(_load(ARCHIVE))
    event = _first_funding(doc["tape"])
    # The first settlement belongs to epoch 94; its window closed long before
    # the 24h boundary, so a receipt stamped there is not that epoch's funding.
    event["ts_ns"] = "1794096000000000000"
    with pytest.raises(AccountingError, match="outside epoch"):
        reconstruct(doc["tape"])


def test_funding_without_an_open_position_is_rejected():
    tape = [
        {"type": "SET_WALLET", "cash": "1000"},
        {"type": "FUNDING", "amount": "-1", "epoch": "1", "settlement_id": "S-1", "ts_ns": "1"},
    ]
    with pytest.raises(AccountingError, match="no open position history"):
        reconstruct(tape)


def test_equal_amounts_across_distinct_settlements_both_survive():
    tape = [
        {"type": "SET_WALLET", "cash": "1000"},
        {"type": "SET_POSITION", "qty": "1", "entry": "100", "epoch": "1"},
        {"type": "FUNDING", "amount": "-1", "epoch": "1", "settlement_id": "SET-A", "ts_ns": "10"},
        {"type": "FUNDING", "amount": "-1", "epoch": "1", "settlement_id": "SET-B", "ts_ns": "20"},
        {"type": "MARK", "price": "100"},
    ]
    out = reconstruct(tape)
    assert out["funding_cash"] == "-2"
    assert out["unexplained_residual"] == "0"


def test_maintenance_margin_crossing_is_a_recorded_limitation():
    # The reconstructor certifies exact cash/position/equity accounting only.
    # Native liquidation and margin-call behaviour is not modelled, so a
    # funding payment that would cross maintenance margin cannot be certified
    # by this oracle. This scoped limitation is recorded in
    # evidence/labd/FUNDING_MUTATIONS.txt.
    assert reconstruct(
        [
            {"type": "SET_WALLET", "cash": "1"},
            {"type": "SET_POSITION", "qty": "1", "entry": "100", "epoch": "1"},
            {"type": "FUNDING", "amount": "-2", "epoch": "1", "settlement_id": "S-M", "ts_ns": "1"},
        ]
    )["unexplained_residual"] == "0"
