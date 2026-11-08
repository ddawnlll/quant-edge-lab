# QEL Implementation Tasks

Status values: `DONE` (acceptance evidence recorded), `IN_PROGRESS`,
`NOT_STARTED`, `BLOCKED`, `RUNNING` (elapsed time not yet satisfied).

| Task | Title | Milestone | Status | Evidence / note |
| --- | --- | --- | --- | --- |
| TASK-01 | Contract SDK and canonical identity | M0 | DONE | Python/Go/Rust validators; `evidence/contract/`; F13, F21, run-key and T-DEDUP vectors |
| TASK-02 | Hand oracle and conformance harness | M0 | DONE | `evidence/conformance/FIXTURE_REPORT.json` 44/44; `MUTATION_REPORT.json` 5/5 caught |
| TASK-03 | Ledger and atomic artifact store | M1 | NOT_STARTED | Go/SQLite with bundled SQLite >= 3.51.3 |
| TASK-04 | Control protocol and durable queue | M1 | NOT_STARTED | Unix framed JSON + localhost HTTP facade |
| TASK-05 | Raw acquisition and source evidence | M1 | NOT_STARTED | Public data only |
| TASK-06 | Normalize and PIT datasets | M1 | NOT_STARTED | DuckDB/Arrow streaming |
| TASK-07 | Typed operators and policy evaluator | M1 | NOT_STARTED | Rust |
| TASK-08 | Native feasibility before backend commitment | M0 | IN_PROGRESS | Nautilus v1.231.0 spike building; lock under `locks/` |
| TASK-09 | Independent accounting implementation | M1 | NOT_STARTED | Python exact integer/Fraction oracle |
| TASK-10 | Venue funding, margin and liquidation profile | M4 | NOT_STARTED | Generic synthetic profile only until verified |
| TASK-11 | Minimum native taker end-to-end slice | M1 | NOT_STARTED | Depends on TASK-08 verdict |
| TASK-12 | Preregistration and walk-forward datasets | M2 | NOT_STARTED | |
| TASK-13 | Matched controls and batch screen | M2 | NOT_STARTED | |
| TASK-14 | Dependent stats and frozen protocols | M3 | NOT_STARTED | |
| TASK-15 | Evaluator principal and private consumption registry | M3 | NOT_STARTED | Separate owner / independent anchor |
| TASK-16 | Signed scientific evaluator | M3 | NOT_STARTED | |
| TASK-17 | Passive queue and composite execution | M4 | NOT_STARTED | Blocked if CAP-L2-QUEUE unresolved |
| TASK-18 | Two-world adapter and queue cross-check | M4 | NOT_STARTED | |
| TASK-19 | Bounded replay and cache purge | M4 | NOT_STARTED | |
| TASK-20 | Crash and recovery completion | M5 | NOT_STARTED | |
| TASK-21 | Agent gateway and CAS task ownership | M5 | NOT_STARTED | |
| TASK-22 | Durable memory projections | M5 | NOT_STARTED | |
| TASK-23 | cgroup and economic admission | M5 | NOT_STARTED | |
| TASK-24 | Operator dashboard and controls | M5 | NOT_STARTED | Bind localhost |
| TASK-25 | Benchmark corpus and performance evidence | M4 | NOT_STARTED | |
| TASK-26 | Statistical null and injection calibration | M3 | NOT_STARTED | |
| TASK-27 | Continuous research soak | M5 | NOT_STARTED | Record RUNNING only for elapsed time |
| TASK-28 | First frozen confirmation cohort | M6 | BLOCKED | Requires fresh OOS, M3/M5 and authorization |
| TASK-29 | Forward calibration protocol and evidence | M7 | BLOCKED | Requires M6 and observed market data |
| TASK-30 | Bot export and rollback package | M7 | BLOCKED | Software may be built; activation requires authorization |
| TASK-31 | One optional kernel challenger | M8 | NOT_STARTED | Optional |
| TASK-32 | Optional second venue or asset mode | M8 | NOT_STARTED | Optional |

## Recorded specification conflicts

None at this checkpoint. Native capability gaps are recorded as
`BLOCKED_BACKEND`, not as specification changes.
