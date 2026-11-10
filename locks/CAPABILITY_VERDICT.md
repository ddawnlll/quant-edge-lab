# TASK-08 · Native feasibility verdict

Date: 2026-11-08. Source: `https://github.com/nautechsystems/nautilus_trader`
at tag `v1.231.0`, commit `27a8e54e7ac3c57d6cbf8891f0283dfbaee97317`.
Crate workspace version `0.61.0`. License LGPL-3.0-or-later; upstream
copyright preserved in the dependency.

## Verified (profile TAKER_L1_FUNDING_STREAMING)

- Pure-Rust release build with no `python` feature and no Python callback.
- `BacktestEngine::run(streaming=true)` + `clear_data()` preserves the open
  position, timer and funding state across input release. Position count was
  1 after chunk 1 and quantity 0.1 at the end.
- `FundingRateUpdate` + `MarkPriceUpdate` produce exactly one funding
  settlement at the boundary (`funding_adjustments = 1`, PnL -0.01 USDT).
- Peak RSS 15.6 MiB, run 23 ms for the spike. Binary 12,079,632 bytes.
  Report hash `5b85c36c...62b0`.

Capabilities: `CAP-TAKER-REPLAY`, `CAP-FUNDING`, `CAP-STREAMING`.

## BLOCKED_BACKEND

The following capabilities are blocked. No fork, monkey patch or invented
method is used.

| Capability | Exact missing hook |
| --- | --- |
| `CAP-LOCAL-VIEW` | Nautilus exposes one shared `Cache` to strategies (`Strategy::cache()`); there is no separate immutable LocalView and no API that prevents native cache access. `run(streaming=true)` also delivers fill callbacks immediately. The two-world split (exchange effect vs delayed local delivery) has no public hook. |
| `CAP-L2-QUEUE` | **PARTIAL after ADR-0003.** The F02 queue-ahead slice is functional: `queue_position(true)` + `trade_execution(true)` + `liquidity_consumption(true)` gives ahead 100, trades 80 then 30 -> exactly 10 fill, with no depth double-consumption; `get_quantity_at_level` is public. Still unproven: public queue-state readback (private field), shared-level budget across orders, unknown-aggressor handling, resync after gaps, marketable-order budget and full L2 depth. |
| `CAP-ISOLATED` | The same shared cache and callback path means a process-level research/evaluator split cannot be proven inside one Nautilus engine. |

Recertified 2026-11-10 by `docs/ADR-0003_l2_queue_recertification.md` and
`evidence/native/FUNCTIONAL_SPIKE_2026-11-09.json`. The queue-ahead F02 slice is
now available to TASK-17 within the stated scope. The two-world/LocalView split
and mark-based liquidation remain blocked. No fork or monkey patch was used.

Consequence: M4 queue claims are limited to the F02 queue-ahead slice; shared
level budget, unknown-aggressor, resync and full L2 depth remain unproven. M1
may use the taker L1 quote/trade slice with funding and the F02 passive queue
slice; it cannot claim full L2, two-world LocalView, passive fill parity beyond
the tested fixtures, or full liquidation fidelity. `RuntimeLock.status` remains
`UNRESOLVED` because the full hook set is not available.

## Reproduction

```sh
cd native/spike
CARGO_TARGET_DIR=/home/quant/quant/.build-cache/qel-native cargo build --release
/home/quant/quant/.build-cache/qel-native/release/qel-native-spike \
  ../../evidence/native/SPIKE_REPORT.json
```
