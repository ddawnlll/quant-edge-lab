# QEL v1.1 final acceptance report

Generated: 2026-11-10. This report is a verification record, not a
profitability claim and not a scientific promotion.

## Closed in this milestone

| Issue | Task | Closure basis | Evidence |
| --- | --- | --- | --- |
| #4 | TASK-11 | Native taker slice is re-verified and F22 now executes on the native replay path through the adapter-managed two-world LocalView. Exchange position/cash updates before response delivery; no policy decision reads a fill before delivery; feed delay is charged once; LocalView converges exactly to the exchange account. | `evidence/labd/LOCAL_VIEW_F22_2026-11-10.json`, `evidence/labd/INTEGRATION_ISSUE04_VERIFY.json` |
| #10 | TASK-17 | All required passive/composite checks have a functional PASS at the declared `ADAPTER_OVER_NATIVE` profile: F02, F03, F04, F05, F06 (native and client), F07, F29, F31, F33, F34. Native gaps are recorded next to each adapter field; no native verdict was upgraded. | `evidence/labd/PASSIVE_QUEUE_ADAPTER_2026-11-10.json`, `evidence/labd/PASSIVE_QUEUE_COMPLETION_2026-11-10.json` |

Both closures depend on the PR merge. The evidence is bound to the tested
source/binary SHA and the pinned Nautilus commit
`27a8e54e7ac3c57d6cbf8891f0283dfbaee97317`.

## Advanced but not closed

| Issue | Status | Exact remaining gate |
| --- | --- | --- |
| #11 | PARTIAL | F22 PASS and the cross-world fill/time-to-delivery summary exist. The independent HftBacktest differential queue adapter (T-QUEUE) over identical command tapes is not implemented; no queue-parity claim is made. |
| #3 | PARTIAL | PIT fee/funding/margin/mark rules and F08-F13/F16 pass. The pinned native path uses entry-based maintenance, a quote-based trigger instead of mark-only, no isolated sub-account and no liquidation fee; venue-certified historical rules are absent. |
| #7, #9, #14 | PARTIAL | Full 1,000-program B6 remains `COMPUTE_BUDGET_INSUFFICIENT` (estimated 1,186.61 core-hours). The pilot is `PILOT_NOT_ACCEPTANCE`. |
| #8 | BLOCKED | `T-ISOLATION` needs administrator-created `qel-research`/`qel-eval` principals, socket ownership and worker network isolation. `scripts/provision_isolation.sh`, `docs/ISOLATION_RUNBOOK.md` and `tests/test_isolation_real.py` are the reproducible resume path. |
| #13 | PARTIAL | B2 recheck PASSES at 26.452 s warm p90 under RP-4G (median 24.292 s, 2 warmups + 12 timed, target 30). B3 on the actual 10M-row atomic L2 corpus is `FAIL_TARGET`: p90 209.741 s vs 120 s (11,247,697 events, 3 timed repeats, RP-4G). B4 100M under RP-8G remains unrun. |
| #15 | PARTIAL | 24h soak PASS; the 7-day stage is still running and B8 is not run. |
| #16 | BLOCKED | `WAITING_FRESH_OOS`: no untouched window, no confirmation authority; the 237-day reserve is below the 250-day primary minimum and the 500-day sensitivity window. |
| #17 | BLOCKED | `INSUFFICIENT_OBSERVATION`: 15 orders/15 fills/1 day vs 200/50/10; no capital authorization. |
| #18 | BLOCKED | `CAPITAL_UNAUTHORIZED`: G5/G6 and live reconnect/rollback evidence require authorized live observations; offline software paths remain testable. |

## First M2 development run

`python -m qel.cli m2-run` is the normal entry point. It validates the frozen
26-month manifest, refuses to substitute mark-only days for execution days,
runs a labelled 3/1/3 development-week fold (train 2024-09-01..03, purge
2024-09-04, validation 2024-09-05..07), fits the 12-variant family on train
only, replays the selected variant on validation, fits 1,000 matched controls
from train-only strata, and runs the frozen paired decision.

The capital-relative notional rule is `notional = capital * risk_fraction`; it
is not a hard-coded 100 USDT. The 20 USDT target is checked against the actual
0.001 BTC venue minimum. At the observed 58,941.89 price the minimum notional is
58.94189 USDT, so a 20 USDT target is `INFEASIBLE_VENUE_MIN_NOTIONAL` and is
reported rather than rounded up.

Evidence: `evidence/labd/M2_DEV_RUN.json` (or the summary in
`evidence/labd/INTEGRATION_V11_FINAL.json`).

Observed result: 12 train variants, train-only selection `STRAT-VAR-10`,
12 validation variants (all 12 retained in the Holm family), 1,000 controls
with 174,000 executed trades, control daily mean -15.20375524 USDT. The frozen
decision returns `INSUFFICIENT_EVIDENCE` with
`STATIONARITY_DIAGNOSTIC_INSUFFICIENT` because the development validation
window has only three shared days (the frozen method requires more). The run is
a pipeline and negative/inconclusive result, not a promotion.

## Test and conformance results

See `evidence/labd/INTEGRATION_V11_FINAL.json` for the exact candidate SHA,
counts, commands and evidence hashes. This milestone does not claim a positive
edge, a fresh OOS result, live authority, or full B6/B7/B8 acceptance.
