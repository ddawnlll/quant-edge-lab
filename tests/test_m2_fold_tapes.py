"""A3: streaming fold-tape builder tests (no network).

The stream builder must emit byte-identical NDJSON to the canonical
``qel.prepare.archive.build`` for the same inputs, including funding rows
and exact tie ordering (quotes/trades, then marks, then funding at equal
timestamps).
"""

from __future__ import annotations

import csv
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from qel.prepare.archive import build as canonical_build  # noqa: E402
import m2_build_fold_tapes_stream as stream  # noqa: E402


def _zip_csv(path: Path, name: str, header: list[str], rows: list[list[str]]) -> None:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(name, buf.getvalue())


def _make_inputs(tmp_path: Path) -> None:
    arch = tmp_path / "arch" / "2024-09-01"
    arch.mkdir(parents=True)
    _zip_csv(
        arch / "BTCUSDT-aggTrades-2024-09-01.zip",
        "BTCUSDT-aggTrades-2024-09-01.csv",
        ["agg_trade_id", "price", "quantity", "first_trade_id", "last_trade_id", "transact_time", "is_buyer_maker"],
        [
            ["1", "100.00", "0.500", "1", "1", "1725148800000", "false"],
            ["2", "100.50", "0.250", "2", "2", "1725148800500", "true"],
            ["3", "101.00", "1.000", "3", "3", "1725148801000", "false"],
            ["4", "101.00", "0.125", "4", "4", "1725148812000", "false"],
        ],
    )
    # One mark exactly at a trade timestamp (tie) and one after the last trade.
    _zip_csv(
        arch / "BTCUSDT-1m-2024-09-01.zip",
        "BTCUSDT-1m-2024-09-01.csv",
        ["open_time", "open", "high", "low", "close", "volume", "close_time"],
        [
            ["1725148800000", "100.00", "101.00", "99.00", "100.75", "1.0", "1725148800000"],
            ["1725148801000", "100.75", "101.00", "100.00", "101.00", "0.5", "1725148859999"],
        ],
    )


def test_stream_output_is_byte_identical_to_canonical(tmp_path: Path) -> None:
    _make_inputs(tmp_path)
    day_dir = tmp_path / "arch" / "2024-09-01"
    funding = [{"fundingTime": "1725148800000", "fundingRate": "0.0001"}]
    # Canonical oracle.
    funding_json = tmp_path / "funding.json"
    funding_json.write_text(json.dumps(funding))
    canon_out = tmp_path / "canon.jsonl"
    canonical_build(
        str(day_dir / "BTCUSDT-aggTrades-2024-09-01.zip"),
        str(day_dir / "BTCUSDT-1m-2024-09-01.zip"),
        str(funding_json),
        str(canon_out),
        "BTCUSDT",
    )
    # Stream builder against patched paths.
    stream.ARCH = tmp_path / "arch"
    stream.OUT = tmp_path / "out"
    stream.OUT.mkdir(exist_ok=True)
    funding_rows = [(1725148800000, "0.0001")]
    entry = stream.build_day("2024-09-01", funding_rows)
    assert entry["status"] == "ok"
    assert entry["quotes"] == 3
    assert entry["trades"] == 4
    assert entry["marks"] == 2
    assert entry["funding"] == 1
    stream_out = tmp_path / "out" / "2024-09-01.jsonl"
    assert stream_out.read_bytes() == canon_out.read_bytes()


def test_tie_order_is_quotes_trades_then_marks_then_funding(tmp_path: Path) -> None:
    _make_inputs(tmp_path)
    stream.ARCH = tmp_path / "arch"
    stream.OUT = tmp_path / "out"
    stream.OUT.mkdir(exist_ok=True)
    stream.build_day("2024-09-01", [(1725148800000, "0.0001")])
    lines = [
        json.loads(line)
        for line in (tmp_path / "out" / "2024-09-01.jsonl").read_text().splitlines()
    ]
    kinds_at_t0 = [row["kind"] for row in lines if row["ts_ns"] == "1725148800000000000"]
    assert kinds_at_t0 == ["Quote", "Trade", "Mark", "Funding"]
    # Timestamps are non-decreasing across the whole file.
    ts = [int(row["ts_ns"]) for row in lines]
    assert ts == sorted(ts)


def test_build_day_is_idempotent(tmp_path: Path) -> None:
    _make_inputs(tmp_path)
    stream.ARCH = tmp_path / "arch"
    stream.OUT = tmp_path / "out"
    stream.OUT.mkdir(exist_ok=True)
    first = stream.build_day("2024-09-01", [(1725148800000, "0.0001")])
    second = stream.build_day("2024-09-01", [(1725148800000, "0.0001")])
    assert first["status"] == "ok"
    assert second["status"] == "skip"


def test_funding_filter_uses_utc_day(tmp_path: Path) -> None:
    # Filtering lives in funding_for_day (called by main), not in build_day.
    rows = [(1725148800000, "0.0001"), (1725235200000, "0.0002")]
    assert stream.funding_for_day(rows, "2024-09-01") == [(1725148800000, "0.0001")]
    assert stream.funding_for_day(rows, "2024-09-02") == [(1725235200000, "0.0002")]
    assert stream.funding_for_day(rows, "2024-09-03") == []


def test_non_monotonic_aggtrades_raises(tmp_path: Path) -> None:
    _make_inputs(tmp_path)
    arch = tmp_path / "arch" / "2024-09-01"
    _zip_csv(
        arch / "BTCUSDT-aggTrades-2024-09-01.zip",
        "BTCUSDT-aggTrades-2024-09-01.csv",
        ["agg_trade_id", "price", "quantity", "first_trade_id", "last_trade_id", "transact_time", "is_buyer_maker"],
        [
            ["1", "100.00", "0.500", "1", "1", "1725148802000", "false"],
            ["2", "100.50", "0.250", "2", "2", "1725148801000", "true"],
        ],
    )
    stream.ARCH = tmp_path / "arch"
    stream.OUT = tmp_path / "out"
    stream.OUT.mkdir(exist_ok=True)
    with pytest.raises(ValueError, match="NON_MONOTONIC_AGGTRADES"):
        stream.build_day("2024-09-01", [(1725148800000, "0.0001")])
    assert not (tmp_path / "out" / "2024-09-01.jsonl").exists()


def test_produced_tape_is_monotonic(tmp_path: Path) -> None:
    _make_inputs(tmp_path)
    stream.ARCH = tmp_path / "arch"
    stream.OUT = tmp_path / "out"
    stream.OUT.mkdir(exist_ok=True)
    entry = stream.build_day("2024-09-01", [(1725148800000, "0.0001")])
    assert entry["status"] == "ok"
    assert entry["funding"] >= 1
    assert entry["funding_day"] == "2024-09-01"
    assert entry["funding_window_ms"] == [1725148800000, 1725235200000]
    lines = [json.loads(line) for line in (tmp_path / "out" / "2024-09-01.jsonl").read_text().splitlines()]
    ts = [int(row["ts_ns"]) for row in lines]
    assert ts == sorted(ts)


def test_build_day_rejects_out_of_day_funding(tmp_path: Path) -> None:
    _make_inputs(tmp_path)
    stream.ARCH = tmp_path / "arch"
    stream.OUT = tmp_path / "out"
    stream.OUT.mkdir(exist_ok=True)
    with pytest.raises(ValueError, match="FUNDING_OUT_OF_DAY"):
        stream.build_day("2024-09-01", [(1725148800000, "0.0001"), (1725235200000, "0.0002")])


def test_build_day_raises_on_missing_funding(tmp_path: Path) -> None:
    _make_inputs(tmp_path)
    stream.ARCH = tmp_path / "arch"
    stream.OUT = tmp_path / "out"
    stream.OUT.mkdir(exist_ok=True)
    with pytest.raises(ValueError, match="FUNDING_GAP"):
        stream.build_day("2024-09-01", [])
    assert not (tmp_path / "out" / "2024-09-01.jsonl").exists()


def test_main_returns_nonzero_when_build_day_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_inputs(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir(exist_ok=True)
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps({"days": {"2024-09-01": {"day": "2024-09-01", "status": "skip"}}}))
    monkeypatch.setattr(stream, "ARCH", tmp_path / "arch")
    monkeypatch.setattr(stream, "OUT", out_dir)
    monkeypatch.setattr(stream, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(stream, "NEEDED", ["2024-09-01"])
    monkeypatch.setattr(stream, "load_funding_rows", lambda days: [(1725148800000, "0.0001")])

    def _boom(day: str, rows: list) -> dict:
        raise RuntimeError("boom")

    monkeypatch.setattr(stream, "build_day", _boom)
    monkeypatch.setattr(sys, "argv", ["m2_build_fold_tapes_stream.py"])
    assert stream.main() != 0
    payload = json.loads(manifest_path.read_text())
    assert payload["built_days"] == 0
    assert payload["days"] == {}
    assert payload["failed_days"] == ["2024-09-01"]
