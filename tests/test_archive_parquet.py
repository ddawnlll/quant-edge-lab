"""P1 archive ZIP -> Parquet: canonical byte parity and fail-closed negatives."""

from __future__ import annotations

import hashlib
import io
import os
import json
import zipfile
from decimal import Decimal
from pathlib import Path

import pytest

from qel.prepare.archive import build as canonical_build
from qel.prepare.archive_parquet import (
    ArchiveParquetError,
    canonical_ndjson_sha,
    convert_archive_day,
    iter_canonical_ndjson_lines,
)

DAY_MS = 1725148800000  # 2024-09-01T00:00:00Z


def _make_zip(path: Path, name: str, header: str, rows: list[str]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(name, header + "\n" + "\n".join(rows) + "\n")
    return path


def _fixture(tmp_path: Path, *, agg_rows=None, mark_rows=None, fund_rows=None):
    agg_rows = agg_rows or [
        f"{DAY_MS + 1000},100.00,0.250,true,1",
        f"{DAY_MS + 1500},59000.00,1.000,false,2",  # trailing-zero price: normalize check
        f"{DAY_MS + 2000},100.10,0.100,true,3",
    ]
    # Real aggTrades header has many columns; DictReader needs ours only.
    agg = _make_zip(
        tmp_path / "agg.zip", "agg.csv",
        "transact_time,price,quantity,is_buyer_maker,agg_trade_id", agg_rows,
    )
    mark_rows = mark_rows or [
        f"{DAY_MS + 1000},100.05",
        f"{DAY_MS + 2000},101.00",
    ]
    mark = _make_zip(tmp_path / "mark.zip", "mark.csv", "close_time,close", mark_rows)
    fund_rows = fund_rows if fund_rows is not None else [f"{DAY_MS + 3600000},0.0001"]
    fund = _make_zip(tmp_path / "fund.zip", "fund.csv", "calc_time,last_funding_rate", fund_rows)
    return agg, mark, fund


def _convert(tmp_path: Path, agg, mark, fund, **kw):
    out = tmp_path / "day.parquet"
    manifest = convert_archive_day(agg, mark, fund, out, day="2024-09-01", **kw)
    return out, manifest


def _canonical_lines(tmp_path: Path, agg, mark, fund) -> list[bytes]:
    # Canonical builder needs a funding JSON file; reuse its slow path on fixture.
    fund_json = tmp_path / "funding.json"
    rows = []
    with zipfile.ZipFile(fund) as z:
        import csv as _csv
        with z.open(z.namelist()[0]) as fh:
            for r in _csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8")):
                rows.append({"fundingTime": r["calc_time"], "fundingRate": r["last_funding_rate"]})
    fund_json.write_text(json.dumps(rows))
    out = tmp_path / "canon.jsonl"
    canonical_build(str(agg), str(mark), str(fund_json), str(out))
    return out.read_bytes().splitlines()


def test_byte_parity_with_canonical_builder(tmp_path):
    agg, mark, fund = _fixture(tmp_path)
    out, manifest = _convert(tmp_path, agg, mark, fund)
    assert manifest["canonical_ndjson_sha256"] == canonical_ndjson_sha(out)
    assert list(iter_canonical_ndjson_lines(out)) == _canonical_lines(tmp_path, agg, mark, fund)
    assert manifest["counts"] == {"Quote": 2, "Trade": 3, "Mark": 2, "Funding": 1}
    assert manifest["rows"] == 8
    # Synthetic L1 label is preserved, never venue depth.
    import pyarrow.parquet as pq
    table = pq.read_table(out)
    assert set(table.column("fidelity").to_pylist()) >= {"assumed_synthetic_l1_around_trade"}


def test_tie_order_quote_trade_before_mark_before_funding(tmp_path):
    ts = DAY_MS + 1000
    agg, mark, fund = _fixture(
        tmp_path,
        agg_rows=[f"{ts},100.00,0.250,true,1"],
        mark_rows=[f"{ts},100.05"],
        fund_rows=[f"{ts},0.0001"],
    )
    out, _ = _convert(tmp_path, agg, mark, fund)
    lines = list(iter_canonical_ndjson_lines(out))
    kinds = [json.loads(l)["kind"] for l in lines]
    # Canonical stable order at equal ts: quote/trade first, then mark, then funding.
    assert kinds.index("Mark") > kinds.index("Trade")
    assert kinds.index("Funding") > kinds.index("Mark")


def test_missing_zip_fails_closed(tmp_path):
    agg, mark, fund = _fixture(tmp_path)
    with pytest.raises(ArchiveParquetError, match="ZIP_MISSING"):
        convert_archive_day(agg, mark, tmp_path / "nope.zip", tmp_path / "x.parquet", day="2024-09-01")


def test_missing_funding_zip_fails_closed(tmp_path):
    agg, mark, _ = _fixture(tmp_path)
    with pytest.raises(ArchiveParquetError, match="FUNDING_ZIP_MISSING|ZIP_MISSING"):
        convert_archive_day(agg, mark, tmp_path / "no-fund.zip", tmp_path / "x.parquet", day="2024-09-01")


def test_source_hash_mismatch_fails_closed(tmp_path):
    agg, mark, fund = _fixture(tmp_path)
    with pytest.raises(ArchiveParquetError, match="SOURCE_HASH_MISMATCH"):
        _convert(tmp_path, agg, mark, fund, expected_source_sha256={"agg_trades": "0" * 64})


def test_decimal_overflow_fails_closed(tmp_path):
    agg, mark, fund = _fixture(
        tmp_path,
        agg_rows=[f"{DAY_MS + 1000},99999999999999999999.00,0.250,true,1"],
    )
    with pytest.raises(ArchiveParquetError, match="DECIMAL_OVERFLOW"):
        _convert(tmp_path, agg, mark, fund)


def test_decimal_precision_fails_closed(tmp_path):
    # Funding rate with 9 decimal places cannot be represented exactly at 1e8.
    agg, mark, fund = _fixture(tmp_path, fund_rows=[f"{DAY_MS + 3600000},0.000000001"])
    with pytest.raises(ArchiveParquetError, match="DECIMAL_PRECISION"):
        _convert(tmp_path, agg, mark, fund)


def test_out_of_order_archive_fails_closed(tmp_path):
    agg, mark, fund = _fixture(
        tmp_path,
        agg_rows=[f"{DAY_MS + 2000},100.10,0.250,true,2", f"{DAY_MS + 1000},100.00,0.250,true,1"],
    )
    with pytest.raises(ArchiveParquetError, match="ARCHIVE_TS_REGRESSION"):
        _convert(tmp_path, agg, mark, fund)


def test_sealed_reserve_refused(tmp_path):
    agg, mark, fund = _fixture(tmp_path)
    with pytest.raises(ArchiveParquetError, match="SEALED_RESERVE_REFUSED"):
        convert_archive_day(agg, mark, fund, tmp_path / "x.parquet", day="2026-03-08")


def test_malformed_csv_fails_closed(tmp_path):
    # agg zip missing required column.
    bad = _make_zip(tmp_path / "bad.zip", "bad.csv", "transact_time,price", [f"{DAY_MS},1"])
    _, mark, fund = _fixture(tmp_path)
    with pytest.raises(ArchiveParquetError, match="CSV_MISSING_COLUMNS"):
        convert_archive_day(bad, mark, fund, tmp_path / "x.parquet", day="2024-09-01")


def test_output_hash_recorded_and_recomputable(tmp_path):
    agg, mark, fund = _fixture(tmp_path)
    out, manifest = _convert(tmp_path, agg, mark, fund)
    assert hashlib.sha256(out.read_bytes()).hexdigest() == manifest["parquet_sha256"]
    assert hashlib.sha256(out.read_bytes()).hexdigest() != manifest["canonical_ndjson_sha256"]


def test_parquet_runner_rejects_chunk_bytes(tmp_path):
    """--chunk-bytes has no meaning for a Parquet tape; it must fail closed.

    Regression for issue #76: the reader used to count one unit per event and
    compare it against a byte budget, silently reinterpreting the flag so a
    differential could flush at different boundaries than the legacy runner.
    """
    import subprocess

    binary = Path(
        os.environ.get(
            "QEL_PARQUET_RUNNER",
            Path(__file__).resolve().parents[1] / "native/runner/target/release/qel-parquet-runner",
        )
    )
    if not binary.is_file():
        pytest.skip(f"parquet runner not built: {binary}")
    agg, mark, fund = _fixture(tmp_path)
    out, _manifest = _convert(tmp_path, agg, mark, fund)
    proc = subprocess.run(
        [
            str(binary),
            "--parquet", str(out),
            "--strategy", str(Path(__file__).resolve().parents[1] / "policy/examples/taker_1m.json"),
            "--out", str(tmp_path / "r.json"),
            "--chunk-bytes", "8388608",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "CHUNK_BYTES_UNSUPPORTED_FOR_PARQUET" in (proc.stderr + proc.stdout)
