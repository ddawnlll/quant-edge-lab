"""Streamed fold driver tests (fake binary, no heavy compute)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from qel.research import m2_stream as MS  # noqa: E402
from qel.research.protocol import ProtocolError  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "tests/stub_fold_builder.py"
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


def test_parallel_seed_aggregation_matches_sequential(tmp_path: Path):
    """Ordered aggregation must reproduce the sequential chain hash."""
    import numpy as _np

    from qel.research import control as C
    from qel.research import control_stream as S

    rng = _np.random.default_rng(3)
    days = []
    start = 1725148800 * 10**9
    for d in range(4):
        ts = (start + d * (86400 * 10**9) + _np.cumsum(rng.integers(1, 4, size=300)) * 10**9).astype(_np.int64)
        levels = 1000 + _np.cumsum(rng.integers(-3, 4, size=300))
        days.append((f"2024-09-0{d + 1}", ts, (levels * 100).astype(_np.int64), ((levels + 1) * 100).astype(_np.int64)))
    train_end = int(days[1][1][-1])
    plan = S.fit_control_plan_stream(days, trades=20, notional="100", seed=0, max_holding_ns=3_600_000_000_000, train_end_ns=train_end)
    sequential = C.StreamingControlAggregator(plan, keep_summaries=True)
    for i in range(12):
        sequential.add(C.run_control_seed(plan, i))
    parallel = C.StreamingControlAggregator(plan, keep_summaries=True)
    MS._aggregate_seeds(plan, parallel, 12, 4)
    assert sequential.summary() == parallel.summary()
    assert sequential.daily_series() == parallel.daily_series()
    assert sequential._chain == parallel._chain


def test_resume_skips_runner_and_verifies_sidecar(tmp_path: Path):
    """A verified day must be skipped without invoking the runner again."""
    _manifest(tmp_path)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    counter = tmp_path / "counter.txt"
    runner = bindir / "counting_runner"
    runner.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        "out = args[args.index('--out') + 1]\n"
        "with open(os.environ['QEL_TEST_COUNTER'], 'a') as fh:\n"
        "    fh.write('run\\n')\n"
        "open(out, 'w').write(json.dumps({'exit_code': 0, 'tape': [], 'total_orders': 0, 'total_positions': 0}))\n"
    )
    runner.chmod(0o755)
    os.environ["QEL_TEST_COUNTER"] = str(counter)
    kwargs = dict(
        manifest_path=str(MANIFEST),
        fold_id="WF-D64AF439CD8D6E7F",
        binary=str(runner),
        strategy_base_path=str(STRATEGY),
        out_dir=str(tmp_path / "out"),
        work_dir=str(tmp_path / "work"),
        builder_script=str(BUILDER),
        train_days_limit=1,
        validation_days_limit=1,
        control_replications=1,
        workers=1,
        day_workers=1,
        seed_workers=1,
    )
    first = MS.run_m2_fold_stream(**kwargs)
    assert first["run_id"]
    invocations = len(counter.read_text().splitlines())
    assert invocations > 0
    MS.run_m2_fold_stream(**kwargs)
    assert len(counter.read_text().splitlines()) == invocations, "resume replayed a verified day"

    # Removing one day's quote sidecar invalidates only that day: it is
    # rebuilt and replayed, and the other day stays skipped.
    per_day = invocations // 2
    assert per_day * 2 == invocations
    (tmp_path / "work" / "quotes" / "2024-09-01.npz").unlink()
    MS.run_m2_fold_stream(**kwargs)
    after = len(counter.read_text().splitlines())
    assert after - invocations == per_day, (after, invocations, per_day)


def test_run_id_pins_research_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Editing a research module must change the run identity."""
    _manifest(tmp_path)
    kwargs = dict(
        manifest_path=str(MANIFEST),
        fold_id="WF-D64AF439CD8D6E7F",
        binary=_fake_binary(tmp_path),
        strategy_base_path=str(STRATEGY),
        out_dir=str(tmp_path / "out"),
        work_dir=str(tmp_path / "work"),
        builder_script=str(BUILDER),
        train_days_limit=1,
        validation_days_limit=1,
        control_replications=1,
        workers=1,
        day_workers=1,
        seed_workers=1,
    )
    first = MS.run_m2_fold_stream(**kwargs)["run_id"]
    params = json.loads((tmp_path / "work" / "run.json").read_text())["params"]
    assert params["research_code_sha256"]["control_stream.py"]
    assert params["funding_source_sha256"]
    original = MS._sha256_file
    monkeypatch.setattr(MS, "_sha256_file", lambda p: "0" * 64 if str(p).endswith("control_stream.py") else original(p))
    second = MS.run_m2_fold_stream(**{**kwargs, "work_dir": str(tmp_path / "work2")})["run_id"]
    assert second != first


def test_pending_seed_window_is_bounded(tmp_path: Path):
    """At most ``window`` seed futures may be in flight at once."""
    import numpy as _np
    from qel.research import control as C
    from qel.research import control_stream as S

    rng = _np.random.default_rng(5)
    days = []
    start = 1725148800 * 10**9
    for d in range(3):
        ts = (start + d * (86400 * 10**9) + _np.cumsum(rng.integers(1, 4, size=200)) * 10**9).astype(_np.int64)
        levels = 1000 + _np.cumsum(rng.integers(-3, 4, size=200))
        days.append((f"2024-09-0{d + 1}", ts, (levels * 100).astype(_np.int64), ((levels + 1) * 100).astype(_np.int64)))
    plan = S.fit_control_plan_stream(days, trades=15, notional="100", seed=0, max_holding_ns=3_600_000_000_000, train_end_ns=int(days[1][1][-1]))
    observed_max = 0
    real_submit = None

    class _SpyPool:
        def __init__(self, *args, **kwargs):
            self._pool = real_pool(*args, **kwargs)

        def __enter__(self):
            self._pool.__enter__()
            return self

        def __exit__(self, *exc):
            return self._pool.__exit__(*exc)

        def submit(self, fn, *args, **kwargs):
            nonlocal observed_max
            observed_max = max(observed_max, len(state["live"]))
            fut = self._pool.submit(fn, *args, **kwargs)
            state["live"].add(fut)
            fut.add_done_callback(lambda f: state["live"].discard(f))
            return fut

        def shutdown(self, *args, **kwargs):
            return self._pool.shutdown(*args, **kwargs)

    state = {"live": set()}
    import concurrent.futures as cf

    real_pool = cf.ProcessPoolExecutor
    cf.ProcessPoolExecutor = _SpyPool  # type: ignore[assignment]
    try:
        aggregator = C.StreamingControlAggregator(plan, keep_summaries=True)
        MS._aggregate_seeds(plan, aggregator, 40, 3, window=4)
    finally:
        cf.ProcessPoolExecutor = real_pool

    assert observed_max <= 4, observed_max
    sequential = C.StreamingControlAggregator(plan, keep_summaries=True)
    for i in range(40):
        sequential.add(C.run_control_seed(plan, i))
    assert aggregator.summary() == sequential.summary()
