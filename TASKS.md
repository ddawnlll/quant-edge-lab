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
| TASK-10 | Venue funding, margin and liquidation profile | PARTIAL | PIT fee/funding/margin-tier/mark/status rules with known_at gating and fail-closed unknown/future rules; funding epoch attribution, duplicate rejection and late receipt; shared-wallet reservations, leverage/tier checks, mark-triggered isolated forced close and exact liquidation cost; F08/F09/F10/F11/F12/F13/F16 10/10 scenarios and exact venue-vs-account reconciliation; no venue-certified historical rules or native liquidation path; `evidence/labd/INTEGRATION_ISSUE03_VERIFY.json` |
| TASK-11 | Minimum native taker end-to-end slice | DONE_WITH_BLOCKED_CAPABILITY | archive day 358,200 events / 307 orders / 154 positions; two funding settlements now on the primary tape with attributable epochs; independent cash/equity/position exact, residual 0; omitted/repeated/late/future-epoch/after-END postings visible or rejected; semantic replay identical; F22 LocalView remains BLOCKED_BACKEND and is not claimed; `evidence/labd/INTEGRATION_ISSUE04_VERIFY.json` |
| TASK-12 | Preregistration and walk-forward datasets | DONE | canonical month-based 12/3/3 expanding folds, final ceil(30%) sealed reserve, purge at label_end >= validation_start, embargo/latency equality, fit/cache scoped to one fold+symbol, deterministic 12-variant preregistration; real BTCUSDT+ETHUSDT 26-month markPriceKlines, 790 complete days, two shared folds, 18/18 scope checks including cross-symbol/validation/reserve rejection; one missing day inside reserve is recorded; intraday aggTrades coverage is 3 months and belongs to TASK-13; `evidence/labd/INTEGRATION_ISSUE05_VERIFY.json` |
| TASK-13 | Matched controls and batch screen | PARTIAL | HMAC_SHA256_COUNTER_V1, 1,000 seeds, one private account per seed, train-only matching, per-stratum diagnostics, deterministic L1 reservoir; 7-day development-week run (10.15M events, 2024-09-01..07) includes exact native-tape funding (147 raw -> 21 unique settlements, long pays positive rate, no future funding), 132,000 trades, zero-trade day retained, 1,000 trials preserved; F27 PASS, B1 PASS from TASK-25, T-PROTOCOL PASS from TASK-12; 100-seed batch/sequential subset equal, full 1,000-seed sequential still running; `evidence/labd/INTEGRATION_ISSUE06_FUNDING_VERIFY.json` |
| TASK-14 | Dependent stats and frozen protocols | DONE | shared-calendar/shared-capital paired decision, one shared stationary-bootstrap index vector per replicate, per-replicate HAC, IUT + Holm over the fixed family, conservative MC bound, WAITING_COMPUTE_BUDGET, primary/sensitivity blocks, power/MDE and reason codes; audit case blocks a +1/day candidate against a +1.5/day control; production `stats-job` returns INSUFFICIENT_EVIDENCE/NO_PROMOTION, never EDGE; full coverage/MDE belongs to TASK-26; `evidence/labd/INTEGRATION_ISSUE07_VERIFY.json` |
| TASK-15 | Evaluator principal and private consumption registry | PARTIAL | independent outside-root anchor chain with fsync/ack, pre-access receipt committing window+alpha, race one-winner, ack-timeout retry spends once, restore/rename/copied-registry refusal, signed batch registry; 18 focused tests and evaluator-demo PASS; actual UID/socket/network isolation remains blocked on administrator-created principals; `evidence/labd/INTEGRATION_ISSUE08_VERIFY.json` |
| TASK-16 | Signed scientific evaluator | DONE | caller-supplied metrics/unsigned code/missing gates/bad lineage/unregistered keys rejected; G0-G4 fail closed; multi-candidate synthetic cohort freeze->consume->execute->one signed batch; invalid candidate stays in the Holm family at p=1; failed gate blocks promotion; exact retry, tamper, duplicate, changed-input and contamination negatives; redacted manifest with independently verified RFC8785-JCS signature; market OOS/G4 remains external; `evidence/labd/INTEGRATION_ISSUE09_VERIFY.json` |
| TASK-17 | Passive queue and composite execution | BLOCKED | CAP-L2-QUEUE missing hook in pinned Nautilus; fixture F02 only |
| TASK-18 | Two-world adapter and queue cross-check | BLOCKED | CAP-LOCAL-VIEW / queue hooks absent |
| TASK-19 | Bounded replay and cache purge | PARTIAL | byte-bounded NDJSON replay with atomic lines, `add_data/run/clear_data`, exact tape/account parity on 240s and archive day including funding, purge of closed orders/positions and active-order/epoch limits with `RESOURCE_LIMIT`; B2 p90 20.5 s and B3 p90 20.5 s pass; B4 11.25M-event peak 44.5 MiB and final-decile slope 0.0502 MiB/M (target 1) pass; B4 100M not run and cgroup memory not delegated, so not certified; `evidence/labd/INTEGRATION_ISSUE12_VERIFY.json` |
| TASK-20 | Crash and recovery completion | DONE | worker killed mid-job, lease requeued, generation 2 completed; `evidence/labd/RECOVERY_REPORT.txt` |
| TASK-21 | Agent gateway and CAS task ownership | DONE | FTS memory, CAS assignment, stale rejection tests; `labd/memory` |
| TASK-22 | Durable memory projections | DONE | rebuild from cited claims; FTS query test |
| TASK-23 | cgroup and economic admission | DONE | delegated cgroup v2; OOM kill, memory/pids/cpu limits and admission tested |
| TASK-24 | Operator dashboard and controls | DONE | localhost dashboard + SSE + health; read-only principal cannot control |
| TASK-25 | Benchmark corpus and performance evidence | PARTIAL | frozen B0/B1/B5 corpora and harnesses; cgroup-v2-enforced RP-4G/RP-8G runs: B0 p90 2.30 ms (target 100) with 0 JIT, B1 1M rows x 8 variants p90 2.66 s (target 10), B5 100k events/1k artifacts with 0 lost commits, 0 torn PASS, read 810 ms and restore 0.47 min; T-RELEASE PASS; B2/B3/B4 not re-run under the enforced profiles and B4 100M remains blocked; `evidence/labd/INTEGRATION_ISSUE13_VERIFY.json` |
| TASK-26 | Statistical null and injection calibration | PARTIAL | repaired actual-cohort B6: no empty-eligible fallback, joint family Holm over 0/1/2 candidates, invalid members retained at p=1, no 16-member padding, per-cohort alpha spending, coverage/power reports and identity/retry checks; corrected pilot (1 program/generator, all 4 generators/effects, 3 cohorts, 1500/500, 1000 controls) is `PILOT_NOT_ACCEPTANCE`; measured 198.3 s/program, full estimate 1,186.61 core-hours (296.65 wall-hours at 4 workers), so the 1000-program FWER certificate remains `COMPUTE_BUDGET_INSUFFICIENT`; `evidence/labd/INTEGRATION_ISSUE14_REPAIR_VERIFY.json` |
| TASK-27 | Continuous research soak | PARTIAL | 24h stage PASS at 2026-11-09T22:32:01Z (86,599s, 1,448 samples, max gap 70s, health OK, 15 sealed outputs / 10 failed / 5 recorded seal failures, no orphan PASS); 7-day stage running, verification wake 2026-11-16T22:26Z; B8 not run; `evidence/labd/SOAK_24H_VERIFY.json`, `evidence/labd/SOAK_24H_VERIFICATION.txt` |
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
