# Evidence invalidation — issue #94 (Mark source)

**Date:** 2026-11-13
**Scope:** development evidence only. No sealed/OOS file was read, moved or rewritten.

## What was wrong

The archive tape builders wrote `Mark` rows from the 1m **trade** kline close
(`.../klines/BTCUSDT/1m/BTCUSDT-1m-<date>.zip`) while labelling them
`observed_mark_price_kline_close`. The real series is
`.../markPriceKlines/BTCUSDT/1m/BTCUSDT-1m-<date>.zip`.

Binance gives both files the same basename, and the Python ingest scripts
(`scripts/m2_ingest_full.py`, `scripts/m2_exec_ingest.py`) downloaded the trade
klines into that name. The Go collector already fetched `markPriceKlines` but
also stored it under the same basename, so the two sources could overwrite each
other.

Measured on the development week (2024-09-01 … 2024-09-07), old vs new mark:

| day | max abs Δ mark (USDT) | mark minutes changed |
|---|---|---|
| 2024-09-01 | 52.78739716 | 1215 / 1440 |
| 2024-09-02 | 112.48683236 | 1236 / 1440 |
| 2024-09-03 | 56.28819858 | 1230 / 1440 |
| 2024-09-04 | 90.74774848 | 1251 / 1440 |
| 2024-09-05 | 63.94247518 | 1216 / 1440 |
| 2024-09-06 | 74.26315624 | 1280 / 1440 |
| 2024-09-07 | 47.76378014 | 1150 / 1440 |

Source: `evidence/labd/MARK_SOURCE_DIFF_2026-11-13.json`.

## Affected tapes (do not treat as mark-observed)

These native tapes were built with the trade-kline close in the `Mark` rows.
Their `archive_native_manifest.json` labels are stale.

| tape | reason |
|---|---|
| `data/native/_fold/2024-09-01.jsonl` … `2024-09-07.jsonl` | stream builder read `BTCUSDT-1m-<date>.zip` (trade klines) as mark |
| `data/native/DS-ARCHIVE-BTCUSDT-2026-11-01` … `2026-11-07` | canonical builder was given the trade kline zip as `--mark` |
| `data/native/DS-ARCHIVE-BTCUSDT-2026-10-29` … `2026-10-31` | same defect, but these dates are in the **sealed reserve**; not read or rebuilt here |

Not affected (verified): `data/native/_dev_w1/*` was built from a real
mark-price archive (spot-checked: 0/1440 differences vs `markPriceKlines`).

## Affected evidence files (listed only — nothing deleted or rewritten)

### Built from `data/native/_fold/*`

- `evidence/labd/M2_FOLD_TAPES_PILOT_2026-11-10.json` — pilot tape hash and counts.
- `evidence/labd/M2_DEV_RUN.json` — fold hashes/counts.
- `evidence/labd/B3_ROOTCAUSE_2026-11-10.json` — replay/throughput evidence over the fold tape.
- `evidence/labd/PERF_V12_INTEGRATION_2026-11-11.json` — legacy NDJSON vs Parquet differential.
- `evidence/labd/PERF_V12_P0_BASELINE_2026-11-11.json` — baseline replay.
- `evidence/labd/PERF_V12_P1_2026-11-11.json` — Parquet parity/perf evidence.

### Built from `data/native/DS-ARCHIVE-BTCUSDT-2026-11-*`

- `evidence/labd/ARCHIVE_NATIVE_MANIFEST.json` — points at the 2026-11-07 tape.
- `evidence/labd/ACCEPTANCE_MATRIX.json`
- `evidence/labd/INTEGRATION_ISSUE04_VERIFY.json`
- `evidence/labd/LOCAL_VIEW_CONTROL_ZERO.json`
- `evidence/labd/LOCAL_VIEW_F22_2026-11-10.json`
- `evidence/labd/LOCAL_VIEW_F22_DECISION_IN_GAP_RUN.json`
- `evidence/labd/LOCAL_VIEW_F22_LARGE_RESPONSE_RUN.json`
- `evidence/labd/LOCAL_VIEW_F22_RUN.json`
- `evidence/labd/LOCAL_VIEW_NATIVE_CONTROL.json`
- `evidence/labd/NATIVE_ARCHIVE_DAY.json`
- `evidence/labd/NATIVE_FUNDING_AUDIT.json`
- `evidence/labd/NATIVE_FUNDING_RUN.json`
- `evidence/research/variants_day/STRAT-VAR-00-report.json` … `STRAT-VAR-11-report.json`

### Related but not a result artifact

- `evidence/labd/TASK10_VENUE_PIT_INVENTORY_2026-11-10.json` — records the old
  label as a claim in code; the inventory itself is correct.
- `scripts/validate_provenance.py` — still compares the 2026-11-07 trade kline
  hash against the stale manifest. It must be pointed at the rebuilt mark file
  when that day is regenerated.
- `evidence/labd/validation-2026-11-10/patches/fix_stale_integration_evidence.patch` —
  historical patch; no action.

## Why these are stale, precisely

Mark affects funding notional, liquidation distance and any accounting that
marks to market. The replay differential on the development week shows the
realized-equity effect is small for `STRAT-VAR-00` (0 on 4 of 7 days; ≤ 0.0074
USDT elsewhere), but that is a property of this strategy, not evidence that the
old tape was correct. Any result that consumed `Mark` must be re-run from a
tape rebuilt with `markPriceKlines`.

## Rebuild rule

Rebuild with the fixed builders; they fail closed with `MARK_SOURCE_MISSING`
instead of falling back to the trade close. The only explicit fallback is
`mark_source="trade_kline_close"`, which writes
`trade_kline_close_proxy_NOT_MARK` and is never the default.
