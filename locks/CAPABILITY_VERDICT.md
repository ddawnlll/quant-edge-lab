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
| `CAP-L2-QUEUE` | `SimulatedExchange` has no per-price queue-ahead model, no own-consumption liquidity budget and no L2 queue API. |
| `CAP-ISOLATED` | The same shared cache and callback path means a process-level research/evaluator split cannot be proven inside one Nautilus engine. |

Consequence: M4 queue/two-world claims remain disabled. M1 may use only the
taker L1 quote/trade slice with funding; it cannot claim L2, queue, passive
fill or full liquidation fidelity. `RuntimeLock.status` remains
`UNRESOLVED` because the full hook set is not available.

## Reproduction

```sh
cd native/spike
CARGO_TARGET_DIR=/home/quant/quant/.build-cache/qel-native cargo build --release
/home/quant/quant/.build-cache/qel-native/release/qel-native-spike \
  ../../evidence/native/SPIKE_REPORT.json
```
