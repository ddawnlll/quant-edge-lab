# Issue triage — 2026-11-12

Rule applied: an issue is closed only when its own acceptance criteria are met
and verified. Related code existing is not sufficient. `NO_EDGE` /
`INSUFFICIENT_EVIDENCE` are legitimate outcomes; they are never "fixed" by
relabelling.

## Scientific blockers (high priority, all remain open)

| Issue | Title | State | Why it stays open |
|---|---|---|---|
| #3 | venue funding, margin, liquidation | **OPEN / PARTIAL** | Inventory (PR #40) is documentation, not venue-certified historical rules. Fee/margin-tier/ADL schedules are not in the public archive. Native liquidation path absent (pinned backend). |
| #7 | paired statistical decision correctness | **OPEN** | Implementation and audit case exist; full-cohort FWER certificate is `COMPUTE_BUDGET_INSUFFICIENT` (~1,186 core-hours). Pilot is not acceptance. |
| #8 | evaluator OS isolation | **OPEN / BLOCKED_AUTHORIZATION** | Code path, runbook and gated test exist. Requires an administrator to create principals; no passwordless sudo. Not claimed. |
| #9 | signed decision release | **OPEN** | Signed-batch path tested on synthetic cohorts; market OOS/G4 is external. |
| #11 | independent queue differential | **OPEN** | F22 passes; the HftBacktest T-QUEUE differential is still unimplemented. |
| #13 | frozen benchmark and release evidence | **OPEN / PARTIAL** | B3 10M atomic L2 is `FAIL_TARGET` (p90 209.741 s vs 120 s). B4 100M under RP-8G unrun. PR #39 explains why but does not fix it. |
| #14 | full null / injected-edge calibration | **OPEN** | B6 1,000-program run is not authorized and not affordable in this window. |
| #15 | soak and recovery evidence | **OPEN** | 7-day soak completes 2026-11-16T22:26Z; B8 unrun. Soak process was left untouched all sprint. |
| #16 | fresh sealed OOS cohort | **OPEN / BLOCKED_AUTHORIZATION** | Needs an untouched window and explicit authorization. Never approached. |
| #17 | forward execution observations | **OPEN** | 15 orders / 15 fills / 1 day vs required 200/50/10. |
| #18 | signed bot export and rollback | **OPEN / BLOCKED_AUTHORIZATION** | Gate refuses `CAPITAL_UNAUTHORIZED`. |

## Resolved this sprint

| Issue | Resolution |
|---|---|
| #90 | **Fixed** in PR #93: build entry points no longer hardcode `/home/quant`; one portable binary resolver; regression test added. macOS/arm64 remain untested and are recorded as untested, not as passing. |
| #76 | **Substantially resolved.** See the funding-positive differential below. Both acceptance halves are addressed; the evidence is recorded in PR #51's evidence file and in this sprint's report. |

### #76 detail — funding-positive differential

Completed 2026-11-12 on non-sealed data.

- Chosen variant/day: `STRAT-VAR-00`, 2024-09-02. Discovered by scanning the
  existing pilot reports: **73 of 180** archived variant-days already carry a
  non-zero funding count, so a funding-positive case was available without any
  new data.
- Per-day tape rebuilt for that day alone (1,193,012 events: 84,710 Quote,
  1,106,859 Trade, 1,440 Mark, 3 Funding), sha256
  `7f4523f8272823375c7fc98e81155c87be5493d929f608e820921e5676857b8b`.
- Rust Parquet ingest of the same day: sha256
  `e51026c9feda2c8571f8a94b35c9e962cb75d5be8bcc229b6abea5dc242435cf`.
- Decode parity: Parquet → canonical NDJSON is **byte-identical** to the
  rebuilt tape (`sha_match: true`, 1,193,012 rows both sides, no divergence).
- Replay differential legacy vs Parquet: **PASS** on every check —
  tape (664 entries both), native account, funding, status, events, iterations,
  order/position totals, engine totals, resource counts. First divergence: none.
  - funding: count **1**, total **0.00021238 USDT** in *both* paths (previously
    only a zero-funding case had been shown).
  - equity: **986.08372330 USDT** in both.
- `--chunk-bytes` semantics: the Parquet reader no longer silently reinterprets
  "bytes" as "events". It now fails closed with
  `CHUNK_BYTES_UNSUPPORTED_FOR_PARQUET: use --chunk <events>`, and the flush
  boundary is driven only by the event count so it matches the legacy runner
  exactly.

## Technical-analysis backlog (#52, #54–#74)

Deferred in full. These are **hypotheses**, not known-profitable strategies, and
each requires validated math plus causal acceptance. Implementing them before
the foundational blockers clear would raise the issue count without producing a
single defensible result. PR #53 stays a draft.

## Performance backlog (#79–#92)

Not implemented, and deliberately so: the measured root cause is documented in
issue #92 and PR #39. The twelve filed micro-optimizations sum to roughly
3–4% of per-event cost; the remaining ~19 µs/event is Nautilus dispatch. Writing
them would be churn that cannot move the 10x goal, and each would need semantic
re-verification.

- #92 **is the root-cause record.** It stays open until a fast path or a written
  decision to keep the engine is produced. A 10x claim needs a tape-identical
  validation corpus first.
- #79–#85, #87–#89: individually real, collectively marginal. Correct order is
  #92 → measured fast path → (only then) the micro list.
- #86 (one fsync per ledger append): a durability/latency trade-off, not a bug.
  Changing it silently would weaken crash-safety evidence.
- #78 (multi-day ingest + CI): partially enabled by PR #51's deterministic
  ingest manifest; a bounded multi-day drive is the natural next step.
- #77 (runner dedup): still valid; `run_parquet` duplicates ~250 lines of setup
  and output. Not done here because it requires byte-identical legacy output
  proof and the window was used for correctness instead.
- #91 (50k→1M events/s roadmap): depends on #92's outcome.
- **New: #94** — the Mark tape is a 1m trade-kline close labelled as an observed
  mark price. Filed this sprint from measured evidence (all 1440 minute closes
  differ on 2024-09-01, max |Δ| 52.79 USDT). This is a scientific provenance
  defect, not a perf item.

## Counting

- Closed/resolved this sprint: **#90** (PR #93), **#76** (evidence in PR #51).
- Left open with a precise reason: 22 issues.
- Newly filed: **#94**.
- Deferred by design: 22 TA21 issues + PR #53.
