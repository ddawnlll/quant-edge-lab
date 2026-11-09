# QEL Implementation Tasks

Status values: `DONE`, `PARTIAL`, `IN_PROGRESS`, `BLOCKED`, `RUNNING`.
`PARTIAL` means the software and its local tests exist but a named capability,
owner separation, or observation window is still blocked. No `PARTIAL` result
is a scientific promotion.

| Task | Title | Status | Evidence / note |
| --- | --- | --- | --- |
| TASK-01 | Contract SDK and canonical identity | DONE | Python/Go/Rust JCS, decimal domains, schemas, run keys; `evidence/contract/` |
| TASK-02 | Hand oracle and conformance harness | DONE | 44/44 scenarios, 10/10 mutation catches including mutated inputs; `evidence/conformance/` |
| TASK-03 | Ledger and atomic artifact store | DONE | crash fault matrix, backup/restore, chain verification including payload content and event-ID recomputation, blob-corruption rejection and reseal repair; `labd/ledger`, `labd/artifacts` tests |
| TASK-04 | Control protocol and durable queue | DONE | 4-byte framing, peer credentials, leases, idempotency, worker path containment and fail-closed traversal jobs; `labd/control`, `labd/queue`, `labd/worker` tests |
| TASK-05 | Raw acquisition and source evidence | DONE | live 20s/240s captures and verified daily archive; collector rejects unsafe symbols/dates; `evidence/labd/LIVE_CAPTURE*.json` |
| TASK-06 | Normalize and PIT datasets | DONE | per-row exact line hash/offset verified for all 12,418 rows with full byte coverage; sequence/snapshot/quote/duplicate/timestamp negatives; bitemporal valid+known-time rules, historical listing/delisting, UNIVERSE_UNVERIFIED gate; DuckDB 2.0.0 reproduces SIGILL (exit 132) and PyArrow/Parquet is the certified path with parity/resource evidence; upstream-response `raw_sha256` is not recomputable from the frame and is explicitly scoped, never hidden; `evidence/labd/INTEGRATION_ISSUE01_VERIFY.json` |
| TASK-07 | Typed operators and policy evaluator | DONE | 26 operators, DAG validation, EMA/STD reference, leakage negatives, direct arithmetic/window/stateful/boolean operator tests; `policy/rust` tests |
| TASK-08 | Native feasibility | DONE_WITH_BLOCKED_CAPABILITY | taker/funding/streaming verified; local clone, remote tag, Cargo.lock, source, binary and spike report hashes re-verified; LocalView/L2 queue/isolation BLOCKED_BACKEND; `locks/`, `evidence/native/PIN_VERIFICATION.json` |
| TASK-09 | Independent accounting implementation | DONE | exact tape reconstruction, residual 0 on native run; `tests/test_accounting.py` |
| TASK-10 | Venue funding, margin and liquidation profile | PARTIAL | generic synthetic margin path and F11/F12 pass; no venue-certified historical tier/liquidation claim |
| TASK-11 | Minimum native taker end-to-end slice | DONE_WITH_BLOCKED_CAPABILITY | archive day 358,200 events / 307 orders / 154 positions; two funding settlements now on the primary tape with attributable epochs; independent cash/equity/position exact, residual 0; omitted/repeated/late/future-epoch/after-END postings visible or rejected; semantic replay identical; F22 LocalView remains BLOCKED_BACKEND and is not claimed; `evidence/labd/INTEGRATION_ISSUE04_VERIFY.json` |
| TASK-12 | Preregistration and walk-forward datasets | PARTIAL | split/control declarations implemented; one full archive day plus 240s live capture, but no multi-day walk-forward evidence |
| TASK-13 | Matched controls and batch screen | PARTIAL | 12 variants + 8 controls matched on calendar/trade count, not holding time; candidate mean -7.16 vs control -14.59, verdict INSUFFICIENT_EVIDENCE |
| TASK-14 | Dependent stats and frozen protocols | DONE | shared-calendar/shared-capital paired decision, one shared stationary-bootstrap index vector per replicate, per-replicate HAC, IUT + Holm over the fixed family, conservative MC bound, WAITING_COMPUTE_BUDGET, primary/sensitivity blocks, power/MDE and reason codes; audit case blocks a +1/day candidate against a +1.5/day control; production `stats-job` returns INSUFFICIENT_EVIDENCE/NO_PROMOTION, never EDGE; full coverage/MDE belongs to TASK-26; `evidence/labd/INTEGRATION_ISSUE07_VERIFY.json` |
| TASK-15 | Evaluator principal and private consumption registry | PARTIAL | independent outside-root anchor chain with fsync/ack, pre-access receipt committing window+alpha, race one-winner, ack-timeout retry spends once, restore/rename/copied-registry refusal, signed batch registry; 18 focused tests and evaluator-demo PASS; actual UID/socket/network isolation remains blocked on administrator-created principals; `evidence/labd/INTEGRATION_ISSUE08_VERIFY.json` |
| TASK-16 | Signed scientific evaluator | PARTIAL | Ed25519 E2E: frozen cohort, pre-access receipt, signed verdict verified, second decision refused; no fresh OOS cohort exists |
| TASK-17 | Passive queue and composite execution | BLOCKED | CAP-L2-QUEUE missing hook in pinned Nautilus; fixture F02 only |
| TASK-18 | Two-world adapter and queue cross-check | BLOCKED | CAP-LOCAL-VIEW / queue hooks absent |
| TASK-19 | Bounded replay and cache purge | PARTIAL | 2,000-event chunks with `clear_data` match the unchunked 15-order/8-position result and 19.9 MiB RSS; TASK-17 dependency remains blocked |
| TASK-20 | Crash and recovery completion | DONE | worker killed mid-job, lease requeued, generation 2 completed; `evidence/labd/RECOVERY_REPORT.txt` |
| TASK-21 | Agent gateway and CAS task ownership | DONE | FTS memory, CAS assignment, stale rejection tests; `labd/memory` |
| TASK-22 | Durable memory projections | DONE | rebuild from cited claims; FTS query test |
| TASK-23 | cgroup and economic admission | DONE | delegated cgroup v2; OOM kill, memory/pids/cpu limits and admission tested |
| TASK-24 | Operator dashboard and controls | DONE | localhost dashboard + SSE + health; read-only principal cannot control |
| TASK-25 | Benchmark corpus and performance evidence | PARTIAL | native spike and job timings recorded; B0-B4 full corpus not run |
| TASK-26 | Statistical null and injection calibration | PARTIAL | four generators × 1,000 programs re-run, 0 promotions each, 95% upper 0.00299; power grid executed (0/30 at 0.0–0.6 daily effect), full MDE/coverage certificate pending |
| TASK-27 | Continuous research soak | RUNNING | systemd `qel-soak` samples every 60s; 24h not yet elapsed |
| TASK-28 | First frozen confirmation cohort | BLOCKED | `WAITING_FRESH_OOS`; no untouched window and no authorization |
| TASK-29 | Forward calibration protocol and evidence | BLOCKED | `INSUFFICIENT_OBSERVATION`; 15 orders/15 fills/1 day vs 200/50/10 |
| TASK-30 | Bot export and rollback package | BLOCKED | gate refuses `CAPITAL_UNAUTHORIZED`; no live capital authorization |
| TASK-31 | Optional kernel challenger | NOT_STARTED | optional M8 |
| TASK-32 | Optional second venue/asset | NOT_STARTED | optional M8 |

## Recorded specification conflicts

- None accepted. Native missing hooks are recorded as `BLOCKED_BACKEND`, not
  silently weakened. DuckDB is an environment limitation, not a spec change.

## Evidence invalidation after the TASK-11 funding/tape fix

TASK-11 changed the native runner's terminal MARK from the last quote midpoint
to the native mark and moved funding onto the primary tape. Any earlier report
derived from a native tape (TASK-13 screen/controls, TASK-19 chunk parity,
TASK-25 timings that consumed the tape, and the archive PnL figures) is stale
until re-run on the integrated runner. The conformance fixtures and the
synthetic statistics calibration are not affected by this change.

