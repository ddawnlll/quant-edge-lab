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
    _make_inputs(tmp_path)
    stream.ARCH = tmp_path / "arch"
    stream.OUT = tmp_path / "out"
    stream.OUT.mkdir(exist_ok=True)
    # A funding row stamped on the next UTC day must not leak into this day.
    rows = [(1725148800000, "0.0001"), (1725235200000, "0.0002")]
    entry = stream.build_day("2024-09-01", rows)
    assert entry["funding"] == 1
