# Resume

Updated: 2026-11-08T22:35Z. Repository:
<https://github.com/ddawnlll/quant-edge-lab> (`main`; see `git log -1` for the current tip
time of writing `079e174`).

## State in one paragraph

The lab is running under user systemd: `qel-qeld` (ledger, queue, localhost
dashboard, control socket), `qel-worker` (cgroup-limited durable jobs) and
`qel-soak` (24h sampler started 2026-11-08T22:21:50Z). TASK-01 through
TASK-09 are implemented; TASK-10 through TASK-19 and M3/M5 are partial or
blocked exactly as recorded in `TASKS.md` and `STATE.json`. No positive edge
exists or is claimed. The last native taker run produced 15 orders and 8
positions, sealed as a job, and independent accounting reproduced equity with
zero residual.

## Verified now

- Spec ZIP SHA-256 `db18e7a6…fd30`; MANIFEST OK; baseline `71628e4b…d06`.
- 44/44 fixture scenarios; 5/5 mutation controls; 34 Python tests; Go
  ledger/artifacts/queue/control/memory/cgroup tests; 3 Rust policy tests.
- cgroup v2 OOM kill and memory/pids/cpu caps measured; worker crash recovered
  as generation 2 after lease requeue.
- B6: four generators × 1,000 programs, 0 promotions each, 95% upper
  0.00299; injected-effect power/MDE grid still open.
- Archive day: 358,200 events, 307 orders, 154 positions, residual 0. The L1
  quote is an explicit synthetic assumption around observed trades.

## Exact next actions

1. **Do not start M6/M7.** TASK-28 remains `WAITING_FRESH_OOS`; TASK-29 is
   `INSUFFICIENT_OBSERVATION`; TASK-30 refuses `CAPITAL_UNAUTHORIZED`.
2. Wait for the soak to reach 24h at **2026-11-09T22:21:50Z**; a scheduled
   verification (sched_42815c00) wakes at **2026-11-09T22:26:00Z**. Check
   `runtime/soak/soak.jsonl` and mark TASK-27 DONE only then.
3. Run the full B6 1000-program calibration when the machine is idle:
   `PYTHONPATH=python .venv/bin/python -m qel.cli calibrate --programs 1000 --out evidence/labd/CALIBRATION_1000.json`
4. Replace the 12-variant development screen with a real matched-control
   family (same calendar, trade count and holding time) and rerun
   `python -m qel.cli screen`.
5. M4 remains blocked. Only select a different backend if it exposes a true
   separate LocalView, delayed delivery and L2 queue hooks; rerun TASK-08 and
   invalidate dependent certificates.
6. OS principal separation needs an administrator: create `qel-research`,
   `qel-eval`, `qel-worker` and move the sealed DB/key/registry to the
   evaluator owner. Record the exact evidence; do not claim isolation before.
7. Forward calibration needs more observed market time and explicit
   authorization; collect data but do not place real-money orders.

## Commands

```sh
cd /home/quant/quant/quant-edge-lab
./scripts/qelctl.sh status
./scripts/qelctl.sh start
./scripts/qelctl.sh restart
./scripts/qelctl.sh stop

export PYTHONPATH=python QEL_ROOT=$PWD
go test ./labd/... -count=1
.venv/bin/python -m pytest -q tests
(cd policy/rust && cargo test --release)
```

## Environment notes

- Go 1.27.3, Rust 1.99.1, Python 3.13.5, 6 vCPU, 11.7 GiB RAM.
- Bundled SQLite 3.54.1 (source `2026-10-16 14:45:20 555e07ef…`); system
  Python SQLite 3.46.1 is below the spec WAL fix, so ledger uses Go bundled.
- DuckDB 2.0.0 aborts with illegal instruction on this CPU; PyArrow 26.0.1 is
  used for Parquet.
- GitHub auth available; public repo pushed under `ddawnlll`.
- No passwordless sudo: OS-owner separation and package-level service changes
  requiring root remain blocked.
