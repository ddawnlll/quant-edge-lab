"""Streamed fold driver tests (fake binary, no heavy compute)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from qel.research import m2_stream as MS  # noqa: E402
from qel.research.protocol import ProtocolError  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "scripts/m2_build_fold_tapes_stream.py"
MANIFEST = ROOT / "evidence/labd/PROTOCOL_MONTHLY_26M.json"
STRATEGY = ROOT / "policy/examples/taker_1m.json"


def _fake_binary(tmp_path: Path) -> str:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    script = bindir / "fake_runner"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "args = sys.argv[1:]\n"
        "out = args[args.index('--out') + 1]\n"
        "report = {'exit_code': 0, 'tape': [], 'total_orders': 0, 'total_positions': 0}\n"
        "open(out, 'w').write(json.dumps(report))\n"
    )
    script.chmod(0o755)
    return str(script)


def _manifest(tmp_path: Path) -> str:
    return str(MANIFEST) if MANIFEST.is_file() else pytest.skip("no frozen manifest on disk")


def test_fold_windows_never_touch_sealed_reserve():
    manifest = json.loads(MANIFEST.read_text())
    train, val, fold = MS.fold_windows(manifest, "WF-D64AF439CD8D6E7F")
    assert fold["fold_id"] == "WF-D64AF439CD8D6E7F"
    assert train[0] == "2024-09-01"
    assert all(d < MS.SEALED_FROM for d in train + val)


def test_unknown_fold_fails_closed():
    manifest = json.loads(MANIFEST.read_text())
    with pytest.raises(ProtocolError):
        MS.fold_windows(manifest, "WF-NOPE")


def test_driver_runs_pilot_and_resumes(tmp_path: Path):
    _manifest(tmp_path)
    binary = _fake_binary(tmp_path)
    kwargs = dict(
        manifest_path=str(MANIFEST),
        fold_id="WF-D64AF439CD8D6E7F",
        binary=binary,
        strategy_base_path=str(STRATEGY),
        out_dir=str(tmp_path / "out"),
        work_dir=str(tmp_path / "work"),
        builder_script=str(BUILDER),
        train_days_limit=1,
        validation_days_limit=1,
        control_replications=2,
    )
    report = MS.run_m2_fold_stream(**kwargs)
    assert report["scope"] == "FULL_FOLD_SCOPE"
    assert report["fold"]["train_dates"] == ["2024-09-01"]
    assert report["fold"]["validation_dates"] == ["2025-09-01"]
    run_id = report["run_id"]
    out_file = tmp_path / "out" / "M2_FOLD_RUN.json"
    assert out_file.is_file()
    # Restart: same identity, days skipped (no new replay needed).
    again = MS.run_m2_fold_stream(**kwargs)
    assert again["run_id"] == run_id
    # Tape is discarded; only sidecars and reports remain.
    tapes_left = sorted((tmp_path / "work/tapes").glob("*.jsonl"))
    assert tapes_left == []
    assert sorted(p.name for p in (tmp_path / "work/quotes").glob("*.npz")) == ["2024-09-01.npz", "2025-09-01.npz"]
    # Checkpoints pin the tape that was verified before discard.
    saved = json.loads((tmp_path / "work/checkpoints.json").read_text())
    assert saved["run_id"] == run_id
    assert saved["days"]["2024-09-01"]["complete"] is True
    assert saved["days"]["2024-09-01"]["tape_sha256"]


def test_run_id_is_deterministic(tmp_path: Path):
    _manifest(tmp_path)
    binary = _fake_binary(tmp_path)
    kwargs = dict(
        manifest_path=str(MANIFEST),
        fold_id="WF-D64AF439CD8D6E7F",
        binary=binary,
        strategy_base_path=str(STRATEGY),
        out_dir=str(tmp_path / "out"),
        work_dir=str(tmp_path / "work"),
        builder_script=str(BUILDER),
        train_days_limit=1,
        validation_days_limit=1,
        control_replications=1,
    )
    first = MS.run_m2_fold_stream(**kwargs)
    second = MS.run_m2_fold_stream(**kwargs)
    assert first["run_id"] == second["run_id"]
