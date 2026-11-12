# Plan — autonomous 3-hour sprint, 2026-11-12

Control loop: INSPECT → PLAN → DELEGATE → IMPLEMENT → TEST → INDEPENDENT REVIEW →
INTEGRATE → RECORD → NEXT TASK.

## Provisional budget vs actual

| Phase | Budget | Actual start | Notes |
|---|---:|---|---|
| Repository audit and dependency inventory | 15 min | 12:37–12:50 | Slower than planned because two independent evidence claims (coverage, venue inventory) were re-derived from disk and HTTP probes rather than trusted. |
| A2/A3 PR integration and foundation fixes | 45 min | 12:50–13:30 | PR #38 and #41 needed real defect fixes before merge; not closed by review alone. |
| Arrow/Parquet/Rust integration | 60 min | 13:10–14:20 | Started early, in parallel with A2/A3 verification, because it is independent. |
| Remaining correctness fixes and bounded benchmarks | 40 min | 14:20–14:50 | Issue #90 landed; others triaged. |
| Final verification and report | 20 min | 14:50–15:30 | Freeze at minute 160 (15:17Z). |

## Work items

1. **PR #38** — verify coverage evidence independently, run focused + full
   suites, fix the reviewer's findings, merge.
2. **PR #41** — same, stacked on #38.
3. **PR #39 / #40** — evidence-only: verify the claims are supportable, merge.
4. **Issue #90** — remove machine-specific paths from build entry points.
5. **PR #47 → #48 → #49 → #51** — verify the reader contract, the baseline
   evidence, the ingest parity and the integrated differential; merge in
   dependency order; then close #50 as superseded.
6. **Issue triage** — classify every open issue; close nothing that has not met
   its own acceptance criteria.
7. **Final report** — honest status for everything touched.

## Explicit non-goals

- No full 456-day A3 run, no B6 1,000-program calibration, no sealed OOS read,
  no live orders, no 14-day A/B benchmark claim, no TA21 implementation.
- No new abstraction layers, no new dependencies, no rewrites.
