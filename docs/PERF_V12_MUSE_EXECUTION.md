# Muse Ember — PERF v1.2 implementation mission

**Mode: orchestrator and coder are both Muse Ember.**
Repo: ddawnlll/quant-edge-lab
Epic: #42 · implementation issues: #43, #44, #45, #46
Foundation draft: #47, branch `perf/v12-columnar-foundation-20261111`
Current experiment stack: #38 → #41; DO NOT merge without required independent review.
Scientific source of truth: immutable checked-in QEL monograph and acceptance contracts.

## Mission

Implement, measure and verify an **Arrow/Parquet-native M2 research
path** while preserving every scientific invariant. This is an
implementation mission, not a document-rewrite exercise.

The normalizer **already emits** PyArrow Parquet. Do not create a
second conflicting PIT pipeline. Target the actual hot path:
Binance archive ZIP/CSV → Python Decimal/JSON → NDJSON →
serde_json/event model → Nautilus. No claim of speedup from
simply wrapping existing JSONL in Parquet.

## Precise execution order

1. **P0 / #43 — anchor and profile.** Read latest `main`,
   PRs #38/#41/#47 and benchmark `bench/language-2026-11`.
   Verify the focused `tests/test_arrow_batches.py` introduced
   by PR #47. Report actual errors and fix only on a separate
   code branch. Pin a 14-day (12-family, 1000 controls) A/B
   reference and per-phase wall/CPU/RSS/disk numbers; do not
   launch the full 456-day run.
2. **P1 / #44 — native data conversion.** Build an additive,
   bounded-memory ZIP/archive-to-Parquet Rust path with exact
   decimals, receipt vs economic time and chronological/tie
   ordinals. Reuse normalized Parquet contracts where possible.
   Use real fundingRate monthly ZIPs, not imagined daily ZIPs.
   Explicitly preserve synthetic-L1 assumption. Hash both
   immutable sources and output. Byte-compare regenerated
   canonical events on fixtures and a day before progressing.
3. **P2 / #45 — native ingestion.** Investigate pinned Nautilus
   Rust API at rev `27a8e54`. Build an opt-in Arrow
   RecordBatch→Data path with no NDJSON primary hot path.
   Keep the existing runner as an independent source-of-truth
   comparison; exercise venue, F-fixtures, F22 LocalView,
   funding, cash/equity/positions, priority ties and
   STOP-FIRST. If a hook is unavailable, report BLOCKED_BACKEND
   rather than modifying the pinned library by stealth.
4. **P3 / #46 — shared vectorized features.** Only share
   deterministic, immutable, PIT-safe computations between
   variants. Keep each strategy's account, order book state,
   fills and positions independent. Candidate screening is
   not a substitute for full Nautilus validation.
5. **P4 / #46 — acceptance.** Under the same benchmark corpus,
   same strategy family, same random controls, and actual
   six-vCPU host, measure 1/2/4/6 worker profiles; bound
   resource use; perform exact scientific and accounting
   diff checks. Target 180 s / 14 days **as a stretch goal**
   and report limitations honestly.

## Hard constraints

- No sealed reserve/OOS read or lookahead.
- No live trading or real capital.
- No B6 full calibration and no A3 full-fold until
  separately authorized.
- Do not weaken fees, funding, slippage, STOP-FIRST,
  liquidation or accounting to appear faster.
- Never relabel synthetic top-of-book as venue-observed depth.
- Only one benchmark-class process group at a time; preserve
  ongoing seven-day soak.
- Target ≤4 GiB RAM and ≤5 GiB working scratch where possible;
  measure actual process-tree/cgroup peak, not speculative RSS.
- Keep immutable evidence, hashes, and all legacy read paths.
- Create small PRs; **do not self-certify independent review**.
- No massive Rust rebuilds for each experiment if narrow
  `cargo check` and shared caches are sufficient.
- No synthetic or toy benchmark presented as live data parity.

## Worker and orchestration contract

- Two lightweight Muse workers maximum. Source-file ownership
  exclusive; day-level benchmark resource globally serialized.
- For each task: scope, base SHA, files owned, tests, negative
  tests, resource ceiling, artifact hashes, acceptance and stop
  conditions.
- Never overwrite old scientific evidence. New evidence gets
  versioned locations and supersession markers when applicable.
- Prepare reviewable PRs, tests and performance evidence.
- Update `reports/orchestration/` with current states and
  owner decisions. Keep blocked work BLOCKED, not DONE.
- Continue independent ready tasks after encountering blockers.

## Required operator report (Turkish)

1. Baseline SHA, open PR stack and worktree.
2. New code and focused tests with actual result.
3. Phase-by-phase wall/CPU/peak memory/disk comparison.
4. Exact differential parity and source lineage.
5. Failed attempts, reverted optimizations, real blockers.
6. Link to focused PR and next executable task.

**Start with P0 and PR #47 review; do not start the full 456-day run.**
