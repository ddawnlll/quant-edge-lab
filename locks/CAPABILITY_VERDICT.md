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
| `CAP-LOCAL-VIEW` | **PARTIAL_ADAPTER_MANAGED after ADR-0004.** Nautilus still exposes one shared `Cache` to strategies and has no backend-native immutable LocalView type. The public `Strategy`/`DataActor` callbacks are sufficient to enforce a two-world split in an adapter: `native/local_view` queues market data to `ts_event + feed_delay_ns` and order/fill responses to `ts_event + response_delay_ns`; the policy reads only delivered `LocalView` state. F22 PASS: 303/303 fills delivered after the exchange update, 0 policy reads before delivery, feed delay charged once, exact cash/position/equity convergence, zero-delay control equals the native runner. No backend-native hook is claimed. |
| `CAP-L2-QUEUE` | **PARTIAL after ADR-0003, the passive-runner follow-up and ADR-0005.** Functional at the declared `ADAPTER_OVER_NATIVE` profile: F02 ahead 100, trades 80 then 30 -> exactly 10 fill, leaves 10, depth-once, cancel remainder, shared level; F03 IOC; F04 explicit `ALREADY_FILLED`; F05 client cancel+new replace with priority reset; F06 native OCO and zero-overfill client OCO; F07 position epochs and stale rejection; F29 book sequence `INVALID`/`RESYNC_REQUIRED`; F31 duplicate/out-of-order suppression and zeroed canceled leaves; F33 FOK/stop-limit gap; F34 partial-entry protection. Still unproven natively: public queue-state readback (private field), unknown-aggressor policy parity, full L2 depth, native priority counter, native epoch field, native last-good sequence and native venue event sequence. |
| `CAP-ISOLATED` | The same shared cache and callback path means a process-level research/evaluator split cannot be proven inside one Nautilus engine. |

Recertified 2026-11-10 by `docs/ADR-0003_l2_queue_recertification.md`,
`docs/ADR-0004_two_world_local_view.md`,
`docs/ADR-0005_client_adapter_semantics.md`, and
`evidence/native/FUNCTIONAL_SPIKE_2026-11-09.json`. The queue-ahead F02 slice is
available to TASK-17 within the stated scope. The two-world/LocalView split is
available at the adapter profile (F22 PASS). Mark-based liquidation remains
blocked. No fork or monkey patch was used.

Follow-up 2026-11-10: `native/passive_runner` reproduces F02 and adds
partial-fill/cancel-remainder, shared-level budget across two passive orders and
two marketable IOC orders, F03 IOC, F06 native OCO, F33 FOK/stop-limit gap and
F34 partial-entry protection. `native/passive_runner/src/adapter.rs` adds the
client-owned F04/F05/F07/F29/F31 and zero-overfill client OCO semantics. The
two-world F22 slice is in `native/local_view`. Evidence:
`evidence/labd/PASSIVE_QUEUE_2026-11-10.json`,
`evidence/labd/PASSIVE_QUEUE_ADAPTER_2026-11-10.json`,
`evidence/labd/LOCAL_VIEW_F22_2026-11-10.json`.

Consequence: M1 may use the taker L1 quote/trade slice with funding and the
adapter-managed F22 two-world path. M4 queue claims are limited to the
`ADAPTER_OVER_NATIVE` profile; native queue-state readback, unknown-aggressor
fill parity, full L2 depth and native priority/epoch/sequence fields remain
unproven. Full liquidation fidelity and `CAP-ISOLATED` remain blocked.
`RuntimeLock.status` remains `UNRESOLVED` because the full native hook set is
not available.

## Reproduction

```sh
cd native/spike
CARGO_TARGET_DIR=/home/quant/quant/.build-cache/qel-native cargo build --release
/home/quant/quant/.build-cache/qel-native/release/qel-native-spike \
  ../../evidence/native/SPIKE_REPORT.json
```
