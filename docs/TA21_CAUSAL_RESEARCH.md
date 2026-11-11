# TA21: causal technical-analysis research migration

**Status: TA0 reference implementation; research only; not integrated into execution.**
Epic: [#52](https://github.com/ddawnlll/quant-edge-lab/issues/52).
Baseline: main at 8b59f795 (2026-11-12).
Source: user-provided 21-framework video breakdown and equations. All asserted edge/probability claims in that source remain unproven.

## Context

QEL already has 26 typed Rust operators (including a pivot operator), canonical contracts, event-driven replay, 12/3/3 walk-forward, sealed 30% reserve, matched HMAC controls and a signed evaluator. PERF v1.2 (#42–#46, PRs #47–#51) adds bounded Arrow/Parquet and shared vectorized features. This PR adds an independent incremental research reference, **not** a second production execution path, not an alternative cache and not an update to the frozen specification.

## Inventory

| # | Framework | TA0 status | Remaining work |
|---|---|---|---|
| 1 | Fibonacci | Partial: confirmed bullish 0.382 retrace | short, deeper ratios, tolerance variants |
| 2 | Breakouts / wedge / triangle / rectangle | Not implemented | causal OLS pivot slopes and contraction |
| 3 | Reversals | Not implemented | double/triple peaks, H&S, cup/handle |
| 4 | Elliott waves | Not implemented | prospective wave-4 detector without future-wave-5 dependency |
| 5 | FVG | Partial: birth plus first mitigation | native replay and gap policy sensitivity |
| 6 | Candlesticks | Partial: engulfing, hammer, shooting star, doji | variations, calibrated tick tolerances |
| 7 | Heikin Ashi | Recursive open/close features | trend classification; never synthetic fills |
| 8 | Moon phases | Approximate synodic cosine feature | null/placebo ablation; no directional default |
| 9 | Renko | Not implemented | close-only non-repainting bricks and zero synthetic fills |
| 10 | Harmonics | Not implemented | confirmed XABCD and preregistered tolerance matrix |
| 11 | Static support/resistance | Not implemented | ATR-relative multi-touch causal clusters |
| 12 | Dynamic support/resistance | Not implemented here | reuse EMA/MA in existing typed DAG |
| 13 | Trendlines | Not implemented | causal confirmed pivot anchors |
| 14 | Gann fan | Not implemented | explicit normalized price/time scale and placebo |
| 15 | Momentum | Not implemented here | MACD, PSAR, Supertrend using shared features |
| 16 | Oscillators | Wilder RSI feature | Stochastic and threshold/crossover strategies |
| 17 | Divergence | Not implemented | oscillator values at causally confirmed extrema |
| 18 | Volume | UTC-day VWAP and rolling RVOL | volume profile, POC, true-trade vs bar proxy |
| 19 | Order blocks | Not implemented | base/displacement and later retest |
| 20 | Market structure | HH/HL, LH/LL, neutral | tie and tolerance sensitivity |
| 21 | BOS / CHoCH | Both event detectors | native delivery/fill and threshold calibration |

## Implemented TA0 rules

The incremental Python reference is python/qel/research/ta21.py, tested by tests/test_ta21.py.

1. Only completed bars, finite positive OHLC, nonnegative volume, and strictly increasing UTC-nanosecond close timestamps are accepted. Signals are observed only at current bar close. Orders cannot fill on the same bar close by assumption: delivery latency and actual execution must be modeled separately.
2. A pivot at bar tau is not available until R *later bars* have closed. Origin and confirmation timestamps are separate. Equal-high/low ties inside the causal window produce no pivot. Current-bar newly confirmed pivots do not retroactively classify current-bar crossings.
3. FVG forms on the third candle, first mitigation can happen only later; TTL is fixed in number of bars. At most 128 unmitigated zones are retained, with deterministic oldest-first eviction.
4. Fibonacci has confirmed low-before-high anchors, an ATR-relative 0.382 tolerance, and one signal per retained impulse. No pivot backdating.
5. RSI is Wilder smoothed, initializes after N changes, maps monotonic up to 100 and flat to 50; VWAP resets at UTC-day boundary; RVOL divides the current closed-bar volume by an N-bar mean including that bar.
6. Heikin Ashi is synthetic; never use it as an executable price. Lunar phase is an exploratory scalar only and emits no buy/sell events.
7. Price features use Python floats for reference parity tests only; exact native tick/fees/funding/accounting stay under QEL execution controls.

## Tested scope and evidence boundary

A local isolated Python 3.13 / pytest harness using the authored module and tests ran 10 focused tests with 10 passes. Cases: malformed bars, duplicate time, exact pivot delay, prefix invariance, FVG mitigation timing, RSI warmup/flat, VWAP reset, RVOL definition, HA/lunar, candle geometry, one-shot Fib and BOS/CHoCH. This is **not** an independent CI run, production Rust parity, or replay verification.

## Follow-on gates

- Review TA0 independently, run full Python suite plus Go and Rust checks on the target Debian host.
- Resolve and merge staged PERF v1.2 changes before adding the feature cache / native bridge; coordinate explicitly with #46 / PR #51.
- Version feature schema, config hash and as-of/delivery clocks; preserve the frozen Rust spec and sealed dataset boundaries.
- Complete deterministic framework families and constrain high-ambiguity Elliott/harmonic/Gann cases. The source wave-4 entry rule cannot require knowing the future wave 5; any future-wave validation must be ex-post labeled.
- Preregister limited hypothesis families and exits before running them. Use realistic fees, funding, slippage, stop-first ties, random-entry controls, paired stats, multi-cohort walk-forward and authorized sealed evaluation.
- Keep NO_EDGE and INSUFFICIENT_EVIDENCE valid outcomes. No real-money orders, live exports or scientific promotion from TA0.

**Not claimed:** all 21 detectors, strategy profitability, certified engine integration, correct exchange fills, long-term memory/performance acceptance, or any positive EDGE result.
