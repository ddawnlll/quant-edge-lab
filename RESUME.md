# Resume — QEL v1.1 completion

Updated: 2026-11-10. Repository:
<https://github.com/ddawnlll/quant-edge-lab>. Integration branch
`qel/v1.1-completion`, based on `ce91981`.

## State

The execution-fidelity milestone is complete at the tested scopes:

- **#4 / TASK-11**: native taker slice plus F22 two-world LocalView.
- **#10 / TASK-17**: passive/composite execution adapter, all required checks
  PASS at the declared `ADAPTER_OVER_NATIVE` profile.
- **#11 / TASK-18**: F22 PASS; the independent HftBacktest queue differential
  (T-QUEUE) remains open.
- First M2 development run: frozen manifest refused for mark-only execution
  days, labelled 3/1/3 development-week fold, train-only 12-variant fit,
  1,000 matched controls, frozen paired decision, capital-relative notional and
  explicit 20 USDT venue infeasibility.

No positive edge, fresh OOS result or live authority is claimed.

## Evidence

| Issue | Evidence |
| --- | --- |
| #4 | `evidence/labd/LOCAL_VIEW_F22_2026-11-10.json`, `evidence/labd/INTEGRATION_ISSUE04_VERIFY.json` |
| #10 | `evidence/labd/PASSIVE_QUEUE_ADAPTER_2026-11-10.json`, `evidence/labd/PASSIVE_QUEUE_COMPLETION_2026-11-10.json` |
| #11 | `evidence/labd/LOCAL_VIEW_F22_2026-11-10.json` (F22 only) |
| #13 | `evidence/labd/B2_RECHECK_2026-11-10.json`, `evidence/labd/B3_10M_ATOMIC_L2_2026-11-10.json` |
| #8 | `scripts/provision_isolation.sh`, `docs/ISOLATION_RUNBOOK.md`, `tests/test_isolation_real.py` |
| M2 | `evidence/labd/M2_DEV_RUN.json`, `evidence/labd/INTEGRATION_V11_FINAL.json` |

## Open gates

1. **#8 / TASK-15**: run `scripts/provision_isolation.sh` as root, restrict the
   worker network with the systemd drop-in, then run
   `QEL_REAL_PRINCIPALS=1 pytest -q tests/test_isolation_real.py`.
2. **#11 / TASK-18**: implement the independent HftBacktest differential queue
   adapter over identical command tapes and compare fill rate, quantity,
   time-to-fill, markout and net economics.
3. **#13 / TASK-25**: B3 on the actual 10M-row atomic L2 corpus misses the 120 s
   target at p90 209.741 s; optimize replay with semantic parity and rerun.
   B4 100M under RP-8G is unrun.
4. **#7/#9/#14**: full 1,000-program B6 remains ~1,186.61 core-hours; the pilot
   is not acceptance.
5. **#15**: 7-day soak completes 2026-11-16T22:26Z; B8 remains.
6. **#3**: native mark-based isolated liquidation and venue-certified historical
   rules remain unavailable.
7. **#16/#17/#18**: fresh OOS, authorized real observations and capital
   authorization remain external.

## Commands

```sh
PYTHONPATH=python QEL_ROOT=$PWD .venv/bin/python -m pytest -q tests
go test ./labd/... -count=1
(cd policy/rust && CARGO_TARGET_DIR=/home/quant/quant/.build-cache/qel-policy cargo test --release)
(cd native/local_view && CARGO_TARGET_DIR=/home/quant/quant/.build-cache/qel-native cargo test --release)
(cd native/passive_runner && CARGO_TARGET_DIR=/home/quant/quant/.build-cache/qel-native cargo test --release)
(cd specification && sha256sum -c MANIFEST.sha256)
.venv/bin/python specification/validation/validate_spec.py --report /tmp/qel_validation_report.json
```

## Environment notes

- Go 1.27.3, Rust 1.99.1, Python 3.13.5, 6 vCPU, 11.7 GiB RAM.
- DuckDB 2.0.0 aborts with SIGILL on this CPU; PyArrow 26.0.1 is the certified
  Parquet path.
- No passwordless sudo: OS principal separation remains administrator-gated.
