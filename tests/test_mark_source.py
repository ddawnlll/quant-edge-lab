"""Issue #94: Mark rows must come from markPriceKlines, never trade klines."""

from __future__ import annotations

import csv
import io
import json
import zipfile
from decimal import Decimal
from pathlib import Path

import pytest

from qel.prepare.archive import (
    MARK_LABEL,
    MARK_PROXY_LABEL,
    MARK_SOURCE,
    MARK_SOURCE_PROXY,
    MarkSourceError,
    build,
    load_mark_rows,
)

DAY_MS = 1725148800000
MARK_HEADER = [
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "count",
    "taker_buy_volume",
    "taker_buy_quote_volume",
    "ignore",
]


def _zip_csv(path: Path, header: list[str], rows: list[list[str]]) -> Path:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(path.stem + ".csv", buf.getvalue())
    return path


def _mark_zip(path: Path, closes: list[str], *, volume: str = "0") -> Path:
    rows = [
        [str(DAY_MS + i * 60_000), close, close, close, close, volume, str(DAY_MS + i * 60_000 + 59_999), volume, "60", "0", "0", "0"]
        for i, close in enumerate(closes)
    ]
    return _zip_csv(path, MARK_HEADER, rows)


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    agg = _zip_csv(
        tmp_path / "agg.zip",
        ["agg_trade_id", "price", "quantity", "first_trade_id", "last_trade_id", "transact_time", "is_buyer_maker"],
        [
            ["1", "100.00", "0.500", "1", "1", str(DAY_MS + 1000), "false"],
            ["2", "100.50", "0.250", "2", "2", str(DAY_MS + 61_000), "true"],
        ],
    )
    mark = _mark_zip(tmp_path / "markPriceKlines.zip", ["100.10", "100.60"])
    funding = tmp_path / "funding.json"
    funding.write_text(json.dumps([{"fundingTime": str(DAY_MS), "fundingRate": "0.0001"}]))
    return agg, mark, funding


def _mark_rows(out: Path) -> list[dict]:
    return [json.loads(line) for line in out.read_text().splitlines() if json.loads(line)["kind"] == "Mark"]


def test_mark_label_and_values_come_from_mark_klines(tmp_path: Path) -> None:
    agg, mark, funding = _inputs(tmp_path)
    out = tmp_path / "tape.jsonl"
    build(str(agg), str(mark), str(funding), str(out))

    marks = _mark_rows(out)
    assert [m["price"] for m in marks] == ["100.1", "100.6"]
    manifest = json.loads((tmp_path / "archive_native_manifest.json").read_text())
    assert manifest["fidelity"]["MARK"] == MARK_LABEL
    assert manifest["mark_source"]["source"] == MARK_SOURCE


def test_trade_kline_input_fails_closed_by_default(tmp_path: Path) -> None:
    agg, mark, funding = _inputs(tmp_path)
    trade = _mark_zip(tmp_path / "tradeKlines.zip", ["100.10", "100.60"], volume="1.0")
    with pytest.raises(MarkSourceError, match="MARK_SOURCE_MISSING"):
        build(str(agg), str(trade), str(funding), str(tmp_path / "tape.jsonl"))


def test_trade_close_proxy_is_explicit_and_never_observed_label(tmp_path: Path) -> None:
    agg, _mark, funding = _inputs(tmp_path)
    trade = _mark_zip(tmp_path / "tradeKlines.zip", ["100.10", "100.60"], volume="1.0")
    out = tmp_path / "tape.jsonl"
    build(
        str(agg),
        str(trade),
        str(funding),
        str(out),
        mark_source=MARK_SOURCE_PROXY,
    )
    manifest = json.loads((tmp_path / "archive_native_manifest.json").read_text())
    assert manifest["fidelity"]["MARK"] == MARK_PROXY_LABEL
    assert MARK_LABEL not in json.dumps(manifest)
    assert all(m["kind"] == "Mark" for m in _mark_rows(out))


def test_missing_mark_minute_fails_closed(tmp_path: Path) -> None:
    closes = [str(Decimal("100") + Decimal(i) / 100) for i in range(1439)]
    mark = _mark_zip(tmp_path / "markPriceKlines.zip", closes)
    with pytest.raises(MarkSourceError, match="expected 1440"):
        load_mark_rows(str(mark), require_full_day=True)


def test_missing_mark_file_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(MarkSourceError, match="MARK_SOURCE_MISSING"):
        load_mark_rows(str(tmp_path / "absent.zip"))


def test_unknown_mark_source_fails_closed(tmp_path: Path) -> None:
    mark = _mark_zip(tmp_path / "markPriceKlines.zip", ["100.10"])
    with pytest.raises(MarkSourceError, match="unknown mark source"):
        load_mark_rows(str(mark), source="klines")
