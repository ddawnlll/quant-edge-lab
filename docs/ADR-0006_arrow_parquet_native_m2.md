# ADR-0006 — Arrow/Parquet-native M2 data and replay path

Status: **PROPOSED / NOT CERTIFIED**
Date: 2026-11-11
Tracks: #42 (epic), #43–#46 (implementation)
Baseline: main `8b59f795663f9e8ba332341a676d389d4e470bf8`
Dependencies: PR #38 (A2 verified archives), PR #41 (streamed A3 runner)

## Decision

Retain QEL's existing validated PIT Parquet normalizer, add a typed,
bounded Arrow processing path into the M2 execution runner, and independently
verify it against the frozen NDJSON/Nautilus replay and accounting reference.

Do **not** replace the reference engine or promote research conclusions until
all differential and independent-review gates pass. Keep the current NDJSON
path available for fixtures, provenance/debugging and scientific verification.
Do not delete immutable raw ZIP/NDJSON evidence.

## Why

`python/qel/prepare/normalize.py` **already produces** certified PyArrow
Parquet from raw captures. However:

- `python/qel/prepare/native_input.py` currently converts Parquet
  → Python `to_pylist` → JSONL again.
- `python/qel/prepare/archive.py` and A3's stacked
  `scripts/m2_build_fold_tapes_stream.py` rebuild archive data with Python
  CSV/Decimal/JSON loops before Rust sees it.
- `native/runner/src/main.rs` parses each NDJSON line with serde_json and
  converts each element to Nautilus model objects; funding additionally
  requires an extra input prepass.
- The 14-day pilot (12 variants + 1000 matched controls) consumed 2025 s;
  per-runner parsing was ~30% of an instrumented single-day replay, while
  `engine.run` was ~62%. **Parquet alone is not an 11× speed guarantee**.

The recently completed Rust/Zig/Goose language benchmark provides no
evidence that rewriting the large Nautilus integration is faster overall.

## Architecture: measured, staged cutover

1. **P0 — instrumentation and schema contract** (#43).
   Pin the golden A3 pilot, source ZIP hashes, strategy configuration and
   binary, benchmark wall/CPU/RSS/disk separately for acquisition,
   normalization, conversion, engine, control seeds and final statistics.
   Instrument nested worker counts and warm-cache vs cold-cache.
2. **P1 — native archive → Parquet** (#44).
   Build exact fixed-point, lossless, source-ordinal-preserving partitions
   from immutable ZIP archives, including observed funding. Use bounded
   Arrow RecordBatches; do not create a full-day NDJSON file in the primary
   path. Reuse existing normalize certificates whenever applicable.
3. **P2 — Arrow → pinned Nautilus** (#45).
   Add an optional Rust adapter consuming typed batches and constructing
   native Quote/Trade/Mark/Funding objects. Preserve complete causal event
   ordering and native execution behavior. Keep the current runner active
   as the comparative oracle. Missing native hooks are BLOCKED_BACKEND.
4. **P3/P4 — shared features and measured 14-day acceptance** (#46).
   Cache immutable indicator inputs where exact and PIT-safe, never share
   account/order/position/queue state between strategy variants. Run a
   bounded 14-day A/B at identical settings and evaluate full scientific
   output equality and resources.

The 180-second target is **a stretch target**; reaching it is neither
assumed nor a reason to weaken semantics. A3 full 456-day and B6 calibration
are not authorized by this ADR.

## Proposed v2 schema, to be frozen only after differential testing

- Partition identity: source SHA256, dataset identity, schema version,
  exchange, symbol, UTC day, source class, event count, event SHA/material.
- Event identity: nanosecond timestamp (checked integer), source ordinal
  and canonical tie rank; equal timestamps must preserve original
  Quote/Trade/Mark/Funding sort behavior, not arbitrary Parquet group order.
- Price/quantity/rate: **fixed-point integer + explicit scale** or
  Arrow Decimal128 only where precision and range are proven. Reject
  overflow, unexpected scale and non-finite inputs; **no float path**.
- Provenance: real trade/mark/funding source identity, raw offset/hash,
  receipt/known-time where available; synthetic L1 is labelled
  `assumed_synthetic_l1_around_trade` permanently.
- Funding: monthly Binance fundingRate archives for A3; deduplicate and
  settle under the frozen epoch/window/sign rules.
- Row groups: bounded memory, deterministic physical ordering, preserved
  total ordering across row groups and day partitions. Record SHA256
  and counts; verify on read.
- Source privacy: sealed reserve is never enumerated, opened or consumed.

This schema is a design contract, **not** a claim that an implemented
canonical v2 Arrow tape is already certified.

## First additive implementation in this PR

`qel.prepare.arrow_batches.iter_verified_pit_batches` reads *existing*
normalized PIT Parquet in bounded Arrow batches, with an independent expected
partition SHA, essential provenance/schema checks, invalid event rejection,
and optional monotonic timestamp checks. It neither creates Python row
objects nor does any native replay; it intentionally stays out of the
production M2 path until P1/P2 are proven.

The first tests cover preserved equal-timestamp iteration, batch boundaries,
hash corruption, missing provenance, invalid kinds, null timestamps,
regressions, invalid batch sizes and missing files. Because A3 archive data
does not yet use this input directly, **there is no speedup claim**.

## Acceptance and abort gates

- Equal canonical business events, ties, funding, exits, fees, orders,
  fills, cash, equity, positions, independent accounting and relevant
  selection/control hashes on fixed and real-day test cases.
- Bounded peak process-tree memory and disk; preserve the running soak.
  Begin with one benchmark-class job at a time, never full A3 without
  operator authorization.
- Record source-specific limits: known synthetic quotes and mark proxies
  remain synthetic/assumptions even if stored in Parquet.
- Scientific contracts, preregistration, sealed-OOS policy and all economic
  thresholds are immutable.
- Any unexplained differential or missing backend halts cutover and
  keeps the existing canonical execution path.
- Independent review required before merging and before claiming a
  performance improvement.

## Independent questions before promotion

1. Does the pinned Nautilus Rust API ingest Arrow-derived native batches
   without a hidden Python/JSON conversion?
2. Which common calculations are actually reusable across 12 isolated
   strategies without changing their observable semantics?
3. Does the Rust Arrow/Parquet dependency tree worsen compile/cache/disk
   enough to offset runtime gains on this specific 6 vCPU / 12 GiB VPS?
4. Does parallelism achieve net throughput gains with the active 7-day soak?
