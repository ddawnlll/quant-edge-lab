# QEL Implementation Tasks

Status values: `DONE`, `PARTIAL`, `IN_PROGRESS`, `BLOCKED`, `RUNNING`.
`PARTIAL` means the software and its local tests exist but a named capability,
owner separation, or observation window is still blocked. No `PARTIAL` result
is a scientific promotion.

| Task | Title | Status | Evidence / note |
| --- | --- | --- | --- |
| TASK-01 | Contract SDK and canonical identity | DONE | Python/Go/Rust JCS, decimal domains, schemas, run keys; `evidence/contract/` |
| TASK-02 | Hand oracle and conformance harness | DONE | 44/44 scenarios, 5/5 mutation catches; `evidence/conformance/` |
| TASK-03 | Ledger and atomic artifact store | DONE | crash fault matrix, backup/restore, chain verification; `labd/ledger`, `labd/artifacts` tests |
| TASK-04 | Control protocol and durable queue | DONE | 4-byte framing, peer credentials, leases, idempotency; `labd/control`, `labd/queue` tests |
| TASK-05 | Raw acquisition and source evidence | DONE | live 20s/240s captures and verified daily archive; `evidence/labd/LIVE_CAPTURE*.json` |
| TASK-06 | Normalize and PIT datasets | PARTIAL | Arrow/Parquet normalization and manifests work; DuckDB 2.0.0 aborts with illegal instruction on this CPU (no AVX2), so the DuckDB path is blocked and PyArrow is used |
| TASK-07 | Typed operators and policy evaluator | DONE | 26 operators, DAG validation, EMA/STD reference, leakage negative cases; `policy/rust` tests |
| TASK-08 | Native feasibility | DONE_WITH_BLOCKED_CAPABILITY | taker/funding/streaming verified; LocalView/L2 queue/isolation BLOCKED_BACKEND; `locks/` |
| TASK-09 | Independent accounting implementation | DONE | exact tape reconstruction, residual 0 on native run; `tests/test_accounting.py` |
| TASK-10 | Venue funding, margin and liquidation profile | PARTIAL | generic synthetic margin path and F11/F12 pass; no venue-certified historical tier/liquidation claim |
| TASK-11 | Minimum native taker end-to-end slice | PARTIAL | 15 orders / 8 positions native run, sealed job, residual 0; F22 LocalView remains blocked |
| TASK-12 | Preregistration and walk-forward datasets | PARTIAL | split/control declarations implemented; only 240s of real data, so no daily walk-forward evidence |
| TASK-13 | Matched controls and batch screen | PARTIAL | 12 real native variants run; controls declared but not executed as a matched control family; verdict INSUFFICIENT_EVIDENCE |
| TASK-14 | Dependent stats and frozen protocols | PARTIAL | HAC, stationary bootstrap, IUT, Holm, power and F27/F28 tests; B6 full 1000-program calibration not reached |
| TASK-15 | Evaluator principal and private consumption registry | PARTIAL | separate private DB, key 0600, pre-access receipt and anchor; distinct OS owner unavailable without sudo |
| TASK-16 | Signed scientific evaluator | PARTIAL | Ed25519 decision signing and verification; no fresh cohort may be evaluated |
| TASK-17 | Passive queue and composite execution | BLOCKED | CAP-L2-QUEUE missing hook in pinned Nautilus; fixture F02 only |
| TASK-18 | Two-world adapter and queue cross-check | BLOCKED | CAP-LOCAL-VIEW / queue hooks absent |
| TASK-19 | Bounded replay and cache purge | PARTIAL | 2,000-event chunks with `clear_data` match the unchunked 15-order/8-position result and 19.9 MiB RSS; TASK-17 dependency remains blocked |
| TASK-20 | Crash and recovery completion | DONE | worker killed mid-job, lease requeued, generation 2 completed; `evidence/labd/RECOVERY_REPORT.txt` |
| TASK-21 | Agent gateway and CAS task ownership | DONE | FTS memory, CAS assignment, stale rejection tests; `labd/memory` |
| TASK-22 | Durable memory projections | DONE | rebuild from cited claims; FTS query test |
| TASK-23 | cgroup and economic admission | DONE | delegated cgroup v2; OOM kill, memory/pids/cpu limits and admission tested |
| TASK-24 | Operator dashboard and controls | DONE | localhost dashboard + SSE + health; read-only principal cannot control |
| TASK-25 | Benchmark corpus and performance evidence | PARTIAL | native spike and job timings recorded; B0-B4 full corpus not run |
| TASK-26 | Statistical null and injection calibration | PARTIAL | 1,000-program AR1 null calibration: 0 promotions, 95% upper 0.00299; t5/common-factor/regime-shift generators still pending |
| TASK-27 | Continuous research soak | RUNNING | systemd `qel-soak` samples every 60s; 24h not yet elapsed |
| TASK-28 | First frozen confirmation cohort | BLOCKED | `WAITING_FRESH_OOS`; no untouched window and no authorization |
| TASK-29 | Forward calibration protocol and evidence | BLOCKED | `INSUFFICIENT_OBSERVATION`; 15 orders/15 fills/1 day vs 200/50/10 |
| TASK-30 | Bot export and rollback package | BLOCKED | gate refuses `CAPITAL_UNAUTHORIZED`; no live capital authorization |
| TASK-31 | Optional kernel challenger | NOT_STARTED | optional M8 |
| TASK-32 | Optional second venue/asset | NOT_STARTED | optional M8 |

## Recorded specification conflicts

- None accepted. Native missing hooks are recorded as `BLOCKED_BACKEND`, not
  silently weakened. DuckDB is an environment limitation, not a spec change.
