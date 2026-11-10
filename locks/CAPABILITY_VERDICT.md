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
| `CAP-L2-QUEUE` | **PARTIAL after ADR-0003 and the passive-runner follow-up.** Functional: `queue_position(true)` + `trade_execution(true)` + `liquidity_consumption(true)` gives F02 ahead 100, trades 80 then 30 -> exactly 10 fill, leaves 10, no depth double-consumption; partial fill + deterministic cancel of the remainder; one shared level budget across two passive orders and two marketable IOC orders; F03 IOC, F06 native OCO, F33 FOK/stop-limit gap and F34 partial-entry protection pass. Still unproven: public queue-state readback (private field), unknown-aggressor handling, resync after gaps, full L2 depth, F04 cancel race, F05 replace priority, F07 epoch safety, F29 book sequence, F31 duplicate-fill injection and client-managed OCO. |
| `CAP-ISOLATED` | The same shared cache and callback path means a process-level research/evaluator split cannot be proven inside one Nautilus engine. |

Recertified 2026-11-10 by `docs/ADR-0003_l2_queue_recertification.md` and
`evidence/native/FUNCTIONAL_SPIKE_2026-11-09.json`. The queue-ahead F02 slice is
now available to TASK-17 within the stated scope. The two-world/LocalView split
and mark-based liquidation remain blocked. No fork or monkey patch was used.

Follow-up 2026-11-10: `native/passive_runner` reproduces F02 and adds
partial-fill/cancel-remainder, shared-level budget across two passive orders and
two marketable IOC orders, F03 IOC, F06 native OCO, F33 FOK/stop-limit gap and
F34 partial-entry protection. Evidence:
`evidence/labd/PASSIVE_QUEUE_2026-11-10.json` and
`evidence/labd/INTEGRATION_ISSUE10_VERIFY.json`. F04/F05/F07/F29/F31 and
client-managed OCO remain unproven.

Consequence: M4 queue claims are limited to the tested F02/F03/F06/F33/F34
slice; unknown-aggressor, resync, full L2 depth, replace priority, epoch safety,
cancel races and duplicate-fill injection remain unproven. M1
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
