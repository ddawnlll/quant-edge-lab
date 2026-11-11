# A3 Streamed Full-Fold Design (bounded memory, bounded disk)

Filed 2026-11-11. Implements the full-fold M2 development execution
without ever holding the fold (456 days, ~39M quotes) in Python objects
or keeping the fold tapes on disk (projected ~90GB vs 66GB free).

## Frozen invariants mapped to the streaming path

| Frozen element | Canonical path | Streamed path (must be identical) |
|---|---|---|
| Fold windows/boundaries | TASK-12 split manifest (365/91, 456/90, purged, sealed from 2026-03-08) | Read from manifest only; sealed dates never in the day set (asserted in code, tested) |
| Family (12 variants) | `screen.variant_grid` + train-only selection (max train PnL, ties by id) | Same functions, same selection rule; per-day replay results aggregated identically |
| Variant replay | `_run_family` runs each variant on each day tape via the native binary; PASS requires tape residual 0 | Same binary invocation per day; same `tape_net_pnl` residual check; day reports kept hashed-small, tape discarded |
| Order of operations | Train all variants, select, then validation | Train days streamed day-by-day (build→verify→replay 12→discard), then validation days; selection still train-only |
| Controls fit | `prepare_control_plan` (strata, targets, calendar, hashes) | Bounded fit producing identical `StratumModel` values, `target_trades_by_day`, `calendar`, `plan_hash`, `selection_plan_hash` (streaming sha256 over the same canonical JSON; differential-tested vs `_hash_json`) |
| Control maths | EMA(20) with 1e-9 rational rounding, per-hour long/directional counts, holding samples from train only (k=2 quote lookahead), median/mean via sorted samples | Same arithmetic, evaluated streaming over numpy arrays in timestamp order; prices as scaled int64 (fail-closed above 2dp / below-at-cents representation guard) |
| Seed draws | HMAC_SHA256_COUNTER_V1, 1,000 seeds, `run_control_seed`, `StreamingControlAggregator` | Same label protocol and seed runner semantics over a lazy plan adapter; aggregator unchanged |
| Funding | Dedup `(ts, rate)`, window `[entry, exit)`, long-pays-positive funding sign, funding_hash committed | Same `normalize_funding`; funding rows from monthly fundingRate zips via the fold tape builder |
| Decision | `stats.family_decision` paired candidate vs controls panel (bootstrap B frozen) | Same function on the same panels |
| Notional | `capital * risk_fraction`; venue minimum feasibility gate (A1: ELIGIBLE at 100/1000) | Same gate, evaluated on the first train quote |

## Bounded fit (why this preserves the protocol)

`prepare_control_plan` materialises 39M `Quote` dataclasses (each with
Fraction bid/ask) plus group index vectors and hashes a 39M-integer
JSON. The streamed fit:

1. During tape build (PR #38 builder), emits per-day quote sidecars
   (`ts` int64, `bid`/`ask` int64 at 2dp scale) in the same sorted
   order `load_tape` produces. Quotes are thus never re-parsed from
   tapes and never become Python objects (39M x 24B = ~0.9GB numpy).
2. Computes strata statistics with counting sorts (holding values and
   inter-quote deltas take a small distinct set; mean/median via exact
   integer value-counts, EMA sequentially with the same 1e-9 rational
   rounding, per-hour counts vectorised).
3. Materialises only the *products* the runners need (day/stratum
   index vectors as numpy ranges/arrays ~0.5GB peak, strata objects
   with lazy sorted holding samples, targets, calendar).
4. Reproduces `plan_hash`/`selection_plan_hash` with a streaming
   sha256 writer producing byte-identical canonical JSON.

The differential bar (on dev-week and synthetic multi-day fixtures):
identical plan hashes, strata, targets, plus identical per-seed daily
series for seeds 0..N. TASK-13's sequential-vs-batch equality
(51 fields, 0 diffs) is the precedent for this bar.

## Pipeline

`python/qel/research/m2_stream.py` (new) + `control_stream.py` (new):

1. `run_m2_fold_stream(...)` — fold driver.
   a. Derive windows from the frozen manifest; refuse any sealed date.
   b. Derive `run_id` deterministically from fold id, windows, family,
      capital/risk, binary sha256, builder code sha256, control params.
   c. For each day (train, then validation): build tape to a scratch
      path, verify sha256 vs builder hash, replay 12 variants,
      verify residuals, write hashed day reports, record checkpoint,
      discard tape. Quote sidecars kept (2MB/day).
   d. Variant aggregation happens in memory across days (small JSONs),
      exactly like `_run_family` totals.
   e. Fit bounded control plan from sidecars (train-only) + canonical
      funding; run 1,000 seeds via lazy plan adapter + aggregator.
   f. Paired decision via `stats.family_decision`; report written with
      `report_hash` (JCS) as in the canonical runner.
2. Checkpoints: `work/run.json` (identity), `work/checkpoints.json`
   (atomic rewrite after each day/phase: tape shas, report statuses and
   shas, fit hashes, seeds completed). Restart re-verifies stored
   report shas and skips confirmed work; a mid-day interruption is
   discarded and redone. No evidence is mutated on resume.
3. The day tape builder is reused from PR #38
   (`scripts/m2_build_fold_tapes_stream.py`) by path import, so the
   stacked branch never duplicates it.

## Cost model (measured in the pilot)

Per-day: tape build (85s 1-worker measured), 12 variant replays
(~26s each 1-worker measured), quote-fit share, seed-run share.
Full forecast then decides whether the complete run fits the current
budget (the forecast is evidence; the full run launches only if the
measured forecast and the scientific contracts permit).

## Negative controls (tested, not assumed)

- Sealed-OOS access: day set derived from manifest minus sealed
  range, asserted in driver and test.
- Funding silence: builder fails loudly when a monthly funding zip is
  unreachable; tape with zero funding rows is rejected for fold days
  (3 settlements/day expected).
- Price scale guard: any quote price not representable at the 2dp
  scale fails the fit closed.
- Checkpoint integrity: corrupted or hash-mismatched reports are
  replayed, never trusted.
- Equality anchors: streamed vs canonical fit hashes and seed series
  on dev-week + synthetic fixtures; streamed vs canonical variant
  day-reports byte-identical modulo timing fields.
