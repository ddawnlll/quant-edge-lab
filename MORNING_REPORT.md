# Morning Report

Status time: 2026-11-08T22:33Z. This is a development and verification report.
It is not a profitability claim and does not certify a trading edge.

## Implemented and verified

| Task | Status | Evidence |
| --- | --- | --- |
| Bootstrap | DONE | ZIP SHA-256 `db18e7a6…fd30`, MANIFEST OK, baseline `71628e4b…d06`, spec validator PASS |
| TASK-01 contract SDK | DONE | Python/Go/Rust JCS, decimals, schemas, run keys; cross-language match |
| TASK-02 fixture oracle | DONE | 44/44 scenarios, 5/5 mutation controls caught |
| TASK-03 ledger/artifacts | DONE | crash matrix, orphan/acked checks, backup restore, hash chain |
| TASK-04 control/queue | DONE | 4-byte framing, peer credentials, leases, idempotency, stale generation |
| TASK-05 public capture | DONE | 20s and 240s live captures; verified daily archive |
| TASK-06 normalize/PIT | PARTIAL | Arrow/Parquet + manifests; DuckDB unusable on this CPU |
| TASK-07 typed policy | DONE | 26 operators, DAG/type/leak checks, EMA48/STD96 reference |
| TASK-08 native feasibility | DONE_WITH_BLOCKED_CAPABILITY | Nautilus v1.231.0 spike; LocalView/L2 queue blocked |
| TASK-09 independent accounting | DONE | exact tape reconstruction; native residual 0 |
| TASK-11 native taker slice | PARTIAL | 15 orders, 8 positions, sealed; F22 blocked |
| TASK-14 dependent stats | PARTIAL | HAC/bootstrap/IUT/Holm/F27/F28; B6 full run pending |
| TASK-15/16 evaluator | PARTIAL | private DB, pre-access receipt, Ed25519 decisions; OS owner split blocked |
| TASK-20 recovery | DONE | worker killed, lease requeued, generation 2 sealed |
| TASK-21/22 memory/agent | DONE | FTS claims, CAS assignment, stale rejection, rebuild |
| TASK-23 cgroup admission | DONE | real OOM kill, memory/pids/cpu caps, heavy-token and disk admission |
| TASK-24 dashboard | DONE | localhost UI, SSE, health, read-only vs write-token split |
| TASK-26 null calibration | PARTIAL | 1,000 AR1 null programs, 0 promotions, 95% upper 0.00299; other generators pending |
| TASK-27 soak | RUNNING | 24h sampler started 22:21:50Z; elapsed time not yet satisfied |

## Active services

| Unit | PID | State | Notes |
| --- | --- | --- | --- |
| `qel-qeld.service` | see `systemctl --user show -p MainPID --value qel-qeld` | active | dashboard `http://127.0.0.1:8787/` |
| `qel-worker.service` | see systemd | active | cgroup-limited job worker |
| `qel-soak.service` | see systemd | active | 60s sampler, `runtime/soak/soak.jsonl` |

Dashboard is bound to `127.0.0.1:8787` only. HTTP writes require the
`runtime/http.token` bearer token (mode 0600); the Unix socket uses
`SO_PEERCRED`.

Health: `curl -s http://127.0.0.1:8787/healthz`
Dashboard snapshot: `curl -s http://127.0.0.1:8787/v1/dashboard`
SQLite: 3.54.1 source `2026-10-16 14:45:20 555e07ef…`.

## Measured resources

- Host: 6 vCPU, 11.7 GiB RAM, 132 GiB free (8% disk used).
- Native spike: 23 ms, 15.6 MiB peak RSS, 12.1 MB release binary.
- Conformance job: cgroup peak 77.4 MiB, pids peak 12, CPU 9.7 s.
- Capture job: cgroup peak 31.5 MiB, pids peak 7.
- Native replay scale: 12,418 events 408 ms / 20 MiB RSS; 124,180 events
  3.15 s / 53 MiB; 1,241,800 events 29.8 s / 367 MiB. The 100x corpus is a
  time-shifted repetition for scale/state growth only. Bounded replay in
  2,000-event chunks with `clear_data` matched the unchunked 15-order /
  8-position result at 19.9 MiB RSS.
- Python tests: 34 passed. Go packages: ledger, artifacts, queue, control,
  memory, cgroup pass. Rust policy: 3 tests pass.

## Running validations

`SOAK-24H` is RUNNING (started 2026-11-08T22:21:50Z). It has not elapsed and
must not be reported as complete. `B6-1000` completed as a bounded AR1 null
calibration (0/1000 promotions, 95% upper 0.00299); it is not the full
four-generator B6 evidence.

## Blockers

1. `CAP-LOCAL-VIEW`, `CAP-L2-QUEUE`, `CAP-ISOLATED`: pinned Nautilus exposes
   one shared cache and no queue-ahead/delayed-local-view hook. M4 is blocked.
2. OS principal separation: no passwordless sudo to create `qel-research`,
   `qel-eval`, `qel-worker`; the private evaluator uses a separate DB and key
   mode 0600 but not a separate Unix owner.
3. DuckDB 2.0.0 aborts with illegal instruction (CPU lacks AVX2); PyArrow is
   used for Parquet.
4. Fresh OOS: no untouched window and no authorization; TASK-28 remains
   `WAITING_FRESH_OOS`.
5. Forward data: 15 orders/15 fills/1 day vs required 200/50/10; TASK-29 is
   `INSUFFICIENT_OBSERVATION`.
6. Export: no capital authorization; TASK-30 refuses with
   `CAPITAL_UNAUTHORIZED`.

## Reproducible operation

```sh
cd /home/quant/quant/quant-edge-lab
./scripts/qelctl.sh start
./scripts/qelctl.sh status
./scripts/qelctl.sh restart
./scripts/qelctl.sh stop
```

Resume after reboot: `./scripts/qelctl.sh start`; the units are enabled and
linger is on. Rebuild: `make contracts`, `go build ./...`,
`(cd policy/rust && cargo test --release)`,
`(cd native/spike && CARGO_TARGET_DIR=/home/quant/quant/.build-cache/qel-native cargo build --release)`,
`(cd native/runner && CARGO_TARGET_DIR=/home/quant/quant/.build-cache/qel-native cargo build --release)`.

## Repository

Public repository: <https://github.com/ddawnlll/quant-edge-lab>
Branch: `main`; remote push succeeded. Final commit is the tip of `main`
after this report update.
Ponytail revision: `96608e078b2d5ea607c5c3b8b5a54d1c1fba6846` (MIT).
Upstream Nautilus revision: `27a8e54e7ac3c57d6cbf8891f0283dfbaee97317` (v1.231.0).

## Remaining issues

- M1/M2/M3 remain PARTIAL because the data window is minutes, not days, and
  matched controls are declared but not executed as a full family.
- B6 1000-program calibration, B0-B4 benchmark corpus, B5 restore drill and
  B7 7-day soak remain open.
- M4 cannot be certified until a backend with LocalView and queue hooks is
  selected; no fork or invented API was used.
- No positive edge exists or is claimed. The 12-variant development screen is
  `INSUFFICIENT_EVIDENCE`; all observed net PnL values are negative.
