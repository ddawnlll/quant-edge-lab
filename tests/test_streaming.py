"""TASK-19 byte-bounded replay parity and resource-limit checks.

These tests exercise the native runner directly. They are skipped when the
release binary or the native datasets are not available in the checkout.

The economic tape and native account snapshot produced by bounded byte replay
must be byte-identical to the materialized reference replay, including funding,
same-timestamp groups, and the final mark/END_FOLD boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

import qel_paths

ROOT = Path(__file__).resolve().parents[1]


def _runner() -> Path | None:
    return qel_paths.find("qel-native-runner")


def _data_root() -> Path:
    return Path(os.environ.get("QEL_DATA_ROOT", ROOT / "data"))


RUNNER = _runner()
DATA = _data_root()
LIVE_240S = DATA / "native" / "DS-BTCUSDT-LIVE-240S" / "events.jsonl"
ARCHIVE = DATA / "native" / "DS-ARCHIVE-BTCUSDT-2026-11-07" / "events.jsonl"

pytestmark = pytest.mark.skipif(
    RUNNER is None,
    reason=qel_paths.describe("qel-native-runner"),
)


def _require(path: Path) -> Path:
    if not path.is_file():
        pytest.skip(f"dataset not available: {path}")
    return path


def _run(tmp_path: Path, events: Path, strategy: str, *extra: str) -> dict:
    out = tmp_path / "out.json"
    command = [
        str(RUNNER),
        "--events",
        str(events),
        "--strategy",
        str(ROOT / "policy" / "examples" / strategy),
        "--out",
        str(out),
        *extra,
    ]
    proc = subprocess.run(command, capture_output=True, text=True, timeout=900)
    assert proc.returncode == 0, proc.stderr[-4000:]
    return json.loads(out.read_text())


def _tape_sha(tape: list[dict]) -> str:
    payload = json.dumps(tape, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _assert_parity(reference: dict, bounded: dict) -> None:
    assert reference["status"] == "PASS"
    assert bounded["status"] == "PASS"
    assert bounded["resource"]["batches"] > 1
    assert bounded["events"] == reference["events"]
    assert bounded["total_orders"] == reference["total_orders"]
    assert bounded["total_positions"] == reference["total_positions"]
    assert bounded["tape"] == reference["tape"]
    assert bounded["native_account"] == reference["native_account"]
    assert bounded["funding"] == reference["funding"]
    assert _tape_sha(bounded["tape"]) == _tape_sha(reference["tape"])


@pytest.mark.parametrize(
    ("events", "strategy", "chunk_bytes"),
    [
        (LIVE_240S, "taker_1s.json", "512"),
        (LIVE_240S, "taker_1s.json", "65536"),
        (ARCHIVE, "taker_1m.json", "8388608"),
    ],
    ids=["live240-b512", "live240-b64k", "archive-b8m"],
)
def test_byte_bounded_replay_matches_materialized_reference(
    tmp_path: Path, events: Path, strategy: str, chunk_bytes: str
) -> None:
    events = _require(events)
    reference = _run(tmp_path, events, strategy)
    bounded = _run(tmp_path, events, strategy, "--chunk-bytes", chunk_bytes)
    _assert_parity(reference, bounded)


def test_sub_line_chunk_budget_never_splits_records(tmp_path: Path) -> None:
    events = _require(LIVE_240S)
    # A byte budget smaller than a single NDJSON record still has to parse every
    # record: the reader only publishes complete lines and completes a
    # same-timestamp group before crossing the boundary.
    bounded = _run(tmp_path, events, "taker_1s.json", "--chunk-bytes", "1")
    assert bounded["status"] == "PASS"
    assert bounded["events"] == 12418
    assert bounded["resource"]["batches"] > 100
    assert bounded["tape"][-1]["type"] == "END_FOLD"


def test_epoch_limit_returns_resource_limit_without_forced_flatten(
    tmp_path: Path,
) -> None:
    events = _require(LIVE_240S)
    bounded = _run(
        tmp_path,
        events,
        "taker_1s.json",
        "--chunk-bytes",
        "65536",
        "--max-epochs",
        "2",
    )
    assert bounded["status"] == "RESOURCE_LIMIT"
    assert bounded["limit"]["reason"] == "MAX_EPOCHS"
    assert bounded["limit"]["observed"] > bounded["limit"]["limit"]
    assert bounded["partial_tape"] is True
    assert bounded["tape"][-1]["type"] != "END_FOLD"
    # Open state is retained, never force-flattened, and no event was skipped
    # silently: the explicit limit receipt reports the processed prefix.
    assert bounded["resource"]["active_positions_end"] <= 1
    assert bounded["resource"]["events_replayed"] < 12418


def test_pending_funding_bound_is_explicit(tmp_path: Path) -> None:
    events = _require(LIVE_240S)
    out = tmp_path / "limited.json"
    command = [
        str(RUNNER),
        "--events",
        str(events),
        "--strategy",
        str(ROOT / "policy" / "examples" / "taker_1s.json"),
        "--out",
        str(out),
        "--max-pending-fundings",
        "5",
    ]
    proc = subprocess.run(command, capture_output=True, text=True, timeout=300)
    assert proc.returncode != 0
    assert "RESOURCE_LIMIT reason=MAX_PENDING_FUNDINGS" in proc.stderr
