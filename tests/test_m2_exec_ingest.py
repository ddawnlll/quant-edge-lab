"""A2: execution-coverage audit tests (no network, no downloads)."""

from __future__ import annotations

import hashlib
import json
import sys
import urllib.request
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import m2_exec_ingest as exec_ingest
import m2_ingest_full as ingest_full
import m2_verify_coverage as verify
from m2_exec_ingest import FOLDS, SEALED_FROM, audit, day_range, day_present


def test_frozen_fold_windows_match_protocol() -> None:
    assert FOLDS[0]["fold_id"] == "WF-D64AF439CD8D6E7F"
    assert FOLDS[0]["train_start"] == "2024-09-01"
    assert FOLDS[0]["validation_end"] == "2025-11-30"
    assert FOLDS[1]["fold_id"] == "WF-2CCA0B04C796FBFC"
    assert SEALED_FROM == "2026-03-08"


def test_fold_windows_never_touch_sealed_reserve() -> None:
    for fold in FOLDS:
        assert fold["validation_end"] < SEALED_FROM
        assert fold["train_end"] < fold["validation_start"]


def test_audit_reflects_disk_without_inventing_coverage() -> None:
    root = Path(__file__).resolve().parents[1]
    evidence_path = root / "evidence" / "labd" / "M2_EXECUTION_COVERAGE.json"
    report = json.loads(evidence_path.read_text())
    assert report["schema"] == "qel.m2_execution_coverage/1.0"
    assert report["sealed_untouched"] is True
    # Independent recount with plain pathlib existence checks over the frozen
    # windows (deliberately not using the production presence predicate,
    # which shares code with the logic under test).
    archive_root = root / "data" / "raw" / "ARCHIVE_BINANCE_USDM" / "BTCUSDT"
    needed: set[str] = set()
    for fold in FOLDS:
        needed.update(day_range(fold["train_start"], fold["train_end"]))
        needed.update(day_range(fold["validation_start"], fold["validation_end"]))
    needed = {d for d in needed if d < SEALED_FROM}
    recount_present = 0
    for d in sorted(needed):
        agg = archive_root / d / f"BTCUSDT-aggTrades-{d}.zip"
        klines = archive_root / d / f"BTCUSDT-1m-{d}.zip"
        if agg.is_file() and klines.is_file():
            recount_present += 1
    assert report["total_needed_days"] == len(needed)
    assert report["present_days"] == recount_present
    assert report["missing_days"] == report["total_needed_days"] - report["present_days"]
    if report["status"] == "COMPLETE":
        assert report["missing_days"] == 0
        assert report["present_days"] == report["total_needed_days"]
    else:
        assert report["present_days"] < report["total_needed_days"]


def test_audit_synthetic_archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    arch = tmp_path / "arch"
    complete_dir = arch / "2024-09-01"
    complete_dir.mkdir(parents=True)
    (complete_dir / "BTCUSDT-aggTrades-2024-09-01.zip").write_bytes(b"agg")
    (complete_dir / "BTCUSDT-1m-2024-09-01.zip").write_bytes(b"1m")
    half_dir = arch / "2024-09-02"
    half_dir.mkdir(parents=True)
    (half_dir / "BTCUSDT-aggTrades-2024-09-02.zip").write_bytes(b"agg")
    monkeypatch.setattr(exec_ingest, "ARCHIVE_ROOT", arch)
    monkeypatch.setattr(exec_ingest, "FOLDS", [{"fold_id": "TEST", "train_start": "2024-09-01", "train_end": "2024-09-01", "validation_start": "2024-09-02", "validation_end": "2024-09-02"}])
    report = exec_ingest.audit()
    assert report["total_needed_days"] == 2
    assert report["present_days"] == 1
    assert report["missing_days"] == 1
    assert report["status"] == "PARTIAL_COVERAGE"
    assert report["missing_first"] == ["2024-09-02"]
    assert report["folds"][0]["train_present"] == 1
    assert report["folds"][0]["validation_present"] == 0


def test_ingest_fetch_one_refuses_sealed_day(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ingest_full, "ARCHIVE_ROOT", tmp_path / "arch")
    with pytest.raises(ValueError, match="SEALED_DAY_REFUSED"):
        ingest_full.fetch_one("2026-03-08", "aggTrades")
    with pytest.raises(ValueError, match="SEALED_DAY_REFUSED"):
        ingest_full.fetch_one("2026-03-09", "1m")
    assert not (tmp_path / "arch" / "2026-03-08").exists()


def test_ingest_main_returns_nonzero_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ingest_full, "ARCHIVE_ROOT", tmp_path / "arch")
    monkeypatch.setattr(ingest_full, "NEEDED", ["2024-09-01"])
    manifest = tmp_path / "manifest.json"
    monkeypatch.setattr(ingest_full, "MANIFEST_PATH", manifest)

    def _boom(url, filename):
        raise RuntimeError("network down")

    monkeypatch.setattr(urllib.request, "urlretrieve", _boom)
    monkeypatch.setattr(ingest_full.time, "sleep", lambda *_args: None)
    assert ingest_full.main() != 0
    report = json.loads(manifest.read_text())
    assert report["failed_days"] == ["2024-09-01"]
    assert report["ingest_hashes"] == {}


def test_ingest_main_writes_ingest_hashes_on_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ingest_full, "ARCHIVE_ROOT", tmp_path / "arch")
    monkeypatch.setattr(ingest_full, "NEEDED", ["2024-09-01"])
    manifest = tmp_path / "manifest.json"
    monkeypatch.setattr(ingest_full, "MANIFEST_PATH", manifest)
    payload = b"fake-zip-content"

    def _fake(url, filename):
        Path(filename).write_bytes(payload)
        return (str(filename), None)

    monkeypatch.setattr(urllib.request, "urlretrieve", _fake)
    monkeypatch.setattr(ingest_full.time, "sleep", lambda *_args: None)
    assert ingest_full.main() == 0
    report = json.loads(manifest.read_text())
    expected_sha = hashlib.sha256(payload).hexdigest()
    assert report["ingest_hashes"]["2024-09-01"]["aggTrades"]["sha256"] == expected_sha
    assert report["ingest_hashes"]["2024-09-01"]["1m"]["sha256"] == expected_sha
    assert report["ingest_hashes"]["2024-09-01"]["aggTrades"]["bytes"] == len(payload)
    assert "days" not in report


def test_verify_coverage_json_contract_two_days(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    arch = tmp_path / "arch"
    for day, kinds in [("2024-09-01", ("aggTrades", "1m")), ("2024-09-02", ("aggTrades",))]:
        d = arch / day
        d.mkdir(parents=True)
        for kind in kinds:
            p = d / f"BTCUSDT-{kind}-{day}.zip"
            with zipfile.ZipFile(p, "w") as z:
                z.writestr(f"BTCUSDT-{kind}-{day}.csv", "a,b\n1,2\n")
    monkeypatch.setattr(verify, "ARCH", arch)
    monkeypatch.setattr(verify, "ROOT", tmp_path)
    monkeypatch.setattr(verify, "NEEDED", ["2024-09-01", "2024-09-02"])
    monkeypatch.setattr(verify, "FOLDS", [{"fold_id": "TEST", "train": ("2024-09-01", "2024-09-01"), "validation": ("2024-09-02", "2024-09-02")}])
    out_path = tmp_path / "coverage.json"
    monkeypatch.setattr(verify, "OUT_PATH", out_path)
    assert verify.main() == 0
    payload = json.loads(out_path.read_text())
    assert payload["total_needed_days"] == 2
    assert payload["present_days"] == 1
    assert payload["missing_days"] == 1
    assert payload["status"] == "PARTIAL_COVERAGE"
    for day in ("2024-09-01", "2024-09-02"):
        assert isinstance(payload["days"][day].get("complete"), bool), day
    assert payload["days"]["2024-09-01"]["complete"] is True
    assert payload["days"]["2024-09-02"]["complete"] is False
    assert payload["days"]["2024-09-01"]["aggTrades"]["present"] is True
    assert payload["days"]["2024-09-02"]["aggTrades"]["present"] is True
    assert payload["days"]["2024-09-02"]["1m"]["present"] is False
    assert payload["folds"][0]["train_present"] == 1
    assert payload["folds"][0]["validation_present"] == 0


def test_day_range_is_contiguous() -> None:
    days = day_range("2024-09-01", "2024-09-07")
    assert days == [f"2024-09-0{i}" for i in range(1, 8)]


def test_day_present_fail_closed_on_missing_dir(tmp_path) -> None:
    assert day_present("2099-01-01") is False


def test_mark_fetch_refuses_sealed_day(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ingest_full, "ARCHIVE_ROOT", tmp_path / "arch")
    with pytest.raises(ValueError, match="SEALED_DAY_REFUSED"):
        ingest_full.fetch_mark_one("2026-03-08")
    assert not (tmp_path / "arch" / "2026-03-08").exists()


def test_mark_fetch_verifies_checksum_and_uses_distinct_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arch = tmp_path / "arch"
    monkeypatch.setattr(ingest_full, "ARCHIVE_ROOT", arch)
    payload = b"real-mark-zip"
    sha = hashlib.sha256(payload).hexdigest()
    urls: list[str] = []

    def _fake(url: str, filename: str) -> tuple[str, None]:
        urls.append(url)
        if url.endswith(".CHECKSUM"):
            Path(filename).write_text(f"{sha}  BTCUSDT-1m-2024-09-01.zip\n")
        else:
            Path(filename).write_bytes(payload)
        return (filename, None)

    monkeypatch.setattr(urllib.request, "urlretrieve", _fake)
    status, size = ingest_full.fetch_mark_one("2024-09-01")
    assert status == "ok"
    assert size == len(payload)
    assert any("markPriceKlines/BTCUSDT/1m" in u for u in urls)
    out = arch / "2024-09-01" / "BTCUSDT-markPriceKlines-1m-2024-09-01.zip"
    assert out.read_bytes() == payload
    # The trade-kline name is never used for mark data.
    assert not (arch / "2024-09-01" / "BTCUSDT-1m-2024-09-01.zip").exists()


def test_mark_fetch_rejects_bad_checksum(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ingest_full, "ARCHIVE_ROOT", tmp_path / "arch")

    def _fake(url: str, filename: str) -> tuple[str, None]:
        if url.endswith(".CHECKSUM"):
            Path(filename).write_text("00" * 32 + "  x.zip\n")
        else:
            Path(filename).write_bytes(b"payload")
        return (filename, None)

    monkeypatch.setattr(urllib.request, "urlretrieve", _fake)
    with pytest.raises(ValueError, match="MARK_CHECKSUM_MISMATCH"):
        ingest_full.fetch_mark_one("2024-09-01")
    assert not (tmp_path / "arch" / "2024-09-01" / "BTCUSDT-markPriceKlines-1m-2024-09-01.zip").exists()
