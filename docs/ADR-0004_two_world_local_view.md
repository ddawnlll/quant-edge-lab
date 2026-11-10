# ADR-0004: Adapter-managed two-world LocalView for F22

Status: PROPOSED (scoped recertification, not a backend-native API claim).
Date: 2026-11-10.
Base: `ce91981055125328dae4b59add8e5432892c4f20`.
Evidence: `evidence/labd/LOCAL_VIEW_F22_2026-11-10.json`.
Raw runs: `evidence/labd/LOCAL_VIEW_F22_RUN.json`,
`evidence/labd/LOCAL_VIEW_F22_LARGE_RESPONSE_RUN.json`,
`evidence/labd/LOCAL_VIEW_F22_DECISION_IN_GAP_RUN.json`,
`evidence/labd/LOCAL_VIEW_CONTROL_ZERO.json`,
`evidence/labd/LOCAL_VIEW_NATIVE_CONTROL.json`.
Crate: `native/local_view/`.
Pinned source: Nautilus `v1.231.0`, commit
`27a8e54e7ac3c57d6cbf8891f0283dfbaee97317`.

## Context

`locks/CAPABILITY_VERDICT.md` records `CAP-LOCAL-VIEW` as `BLOCKED_BACKEND`
because the pinned Nautilus build exposes one shared `Cache` to strategies and
no immutable LocalView or delayed-delivery API. `F22` therefore remained
`DEFINED_NOT_EXECUTED` and TASK-18 was blocked.

The specification does not require a backend-native LocalView type. It
requires two information worlds with causal delivery (`REQ-020`, `INV-04`):

- exchange world `X` applies market data, matches orders and updates the
  account/position immediately;
- strategy world `Y` sees market data after `L_feed` and order/fill responses
  after `L_response`;
- the policy may read only delivered `Y` state.

The pinned public `Strategy`/`DataActor` callback surface is sufficient to
build `Y` in an adapter without forking, monkey-patching, or inventing an API.
The open question was whether the adapter can enforce the read boundary and
prove the timing invariants. `native/local_view` now does that for a tested
profile.

## Decision

`native/local_view` implements `Y` inside the strategy wrapper.

### Public API used

- `nautilus_backtest::engine::BacktestEngine`, `SimulatedVenueConfig`,
  `InstrumentAny::CryptoPerpetual`.
- `nautilus_trading::{Strategy, StrategyCore}`, `nautilus_strategy!`,
  `Strategy::order`, `Strategy::submit_order`.
- `nautilus_common::actor::DataActor` callbacks: `on_start`, `on_quote`,
  `on_trade`, `on_mark_price`, `on_funding_rate`, `on_stop`.
- `Strategy` callbacks: `on_order_filled`, `on_position_opened`,
  `on_position_changed`, `on_position_closed`.
- `nautilus_common::msgbus::subscribe_position_events` for
  `PositionAdjusted(Funding)`, because the pinned build short-circuits that
  variant before `Strategy::on_position_event` (the same workaround is already
  used by `native/runner`).
- `qel_policy::{parse_strategy, Engine::step}` for the typed DAG.

No private symbol is used. The evidence binder records the source, binary,
Cargo manifest, corpus and strategy SHA-256 values.

### Adapter-managed versus backend-native

Backend-native (unchanged Nautilus behaviour):

- order matching and fill generation;
- account and position updates at the exchange timestamp;
- funding settlement and mark-based valuation;
- position/fill event construction and timestamps.

Adapter-managed (`native/local_view`):

- the feed queue and its `deliver_at = ts_event + feed_delay_ns` timestamps;
- the response queue and its `deliver_at = ts_event + response_delay_ns`
  timestamps;
- the `LocalView` cash/position/entry/fee/funding state;
- the policy invocation cadence and the decision record;
- the delivery audit (first-tick check, lateness, time-to-delivery).

Not backend-native:

- there is no engine-level immutable view type;
- the compiler cannot prevent a future edit from calling `self.cache()` in
  `decide`; the boundary is a reviewable code boundary in one function plus a
  static test that fails if `cache()` appears in the policy path;
- the engine has no generic `deliver_at` timer hook, so delivery happens on
  the first later subscribed data callback rather than at the exact nanosecond.

This is the honest scope of the recertification. It is not a claim that
Nautilus now provides `CAP-LOCAL-VIEW` as a backend primitive.

### Delivery semantics

$$t_{seen} = ts_{event} + L_{feed}$$

$$t_{known} = ts_{venue\_response} + L_{response}$$

- A feed item is enqueued in the same callback that receives it and is
  eligible for delivery on that tick.
- A fill/funding response is produced during venue settlement after the
  callback's advance, so it is first eligible on the next data tick. A
  same-timestamp next callback is allowed.
- At a delivery tick, responses are applied before feed. A decision triggered
  by a newly delivered quote therefore sees every response with
  `delivered_at <= decision_ts`, and nothing newer.
- `Nautilus` has no generic timer. The first-tick audit asserts that no
  earlier subscribed data tick existed at or after `deliver_at`. The minimum
  observed total delay is exactly `feed_delay_ns`; the excess is the event
  gap to the next tick, never a second configured delay.
- `LocalView` is flushed at `on_stop` so end-of-replay position/cash
  convergence includes responses that were still in flight.

### Policy read boundary

`decide` reads `self.local` only:

- `last_mid`, `last_trade`, `last_mark`;
- delivered `position_qty`, `entry_price`, `cash`, `fees`, `funding_cash`;
- delivered `position_id` and `position_open_ns`.

It does not call `self.cache()` and does not read the exchange account or
position. Exchange snapshots for evidence are taken in the runner after the
replay and through position/fill callbacks, never as policy input.

## Observed evidence (2026-11-10)

Corpus: `data/native/DS-ARCHIVE-BTCUSDT-2026-11-07/events.jsonl`
(SHA-256 `c7a4c79484a611820c2e314f48397de299294b009d24766ad5c2d03cc5ee22ed`,
358,200 events). Strategy: `policy/examples/taker_1m.json`.

| Run | Feed | Response | Events | Orders | Fills | Positions | Convergence |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| control | 0 | 0 | 358,200 | 307 | 307 | 154 | cash/equity/position diff 0 |
| main | 50 ms | 25 ms | 358,200 | 303 | 303 | 152 | diff 0 |
| large response | 50 ms | 30 s | 358,200 | 303 | 303 | 152 | diff 0 |
| decision in gap | 50 ms | 90 s | 358,200 | 412 | 294 | 128 | diff 0 |

Independent exact accounting (`qel.accounting.core.reconstruct` /
`python -m qel.cli accounting`):

- every exchange tape has `unexplained_residual == 0` and matches the native
  account cash, equity and position exactly;
- every delivered-local tape has `unexplained_residual == 0` and matches the
  `LocalView` cash, equity and position exactly.

F22 invariants:

1. Exchange update precedes response delivery. In the delayed runs every fill
   has `delivered_at > exchange_ts`. The control has 305/307 strictly later
   and 2 delivered on a same-timestamp next callback; no fill is delivered
   before its exchange update.
2. No policy decision uses a fill before delivery. `policy_reads_before_delivery == 0`
   in all runs. In the 90 s run, 294 fills have an intervening decision and 294
   decisions have a pre-existing in-flight fill (max in-flight quantity
   0.002); every such decision's `LocalView` position equals the sum of
   delivered fills only. The 30 s run has zero decisions inside any delivery
   gap, which is the literal no-decision-in-gap check.
3. Feed delay is charged once. `first_tick_violations == 0`,
   `delivered_before_due == 0`, and `min_total_delay_ns == feed_delay_ns`
   (50,000,000 ns main; 0 control). Excess lateness is the observed event gap
   to the first later data tick.
4. `LocalView` converges to the exchange account exactly at end of replay:
   cash, equity and position differences are `0` in all runs.
5. Zero-delay control equals the existing native runner. The economic tape is
   field-identical for all 307 fills (`ts_ns`, side, qty, price, fee), both
   funding postings and the mark; the native account snapshot is identical.
   The only expected difference is delivery timing inside the adapter (next
   callback rather than synchronous settlement); no order-timing difference
   was observed.

Resource envelope: peak RSS 89,792-92,844 KB (about 90 MiB), run time
6.2-9.3 s per full archive replay. This is inside the 2 GiB slot.

## Recertification impact

- **TASK-08**: the public callback surface is sufficient for an
  adapter-managed two-world slice. No backend-native immutable view exists.
- **TASK-11**: F22 is demonstrated for the taker L1 archive profile. The
  existing taker/funding certificate is unchanged; this ADR does not modify
  `native/runner` or its evidence.
- **TASK-18**: `native/local_view` provides the delayed-delivery half. The
  HftBacktest queue cross-check and queue parity remain outside this evidence.
- **CAP-LOCAL-VIEW**: recertified from `BLOCKED_BACKEND` to **PARTIAL** only
  for the proven F22 profile (BTCUSDT L1 quote/trade/mark/funding archive,
  single instrument, netting margin account, market IOC taker orders). It is
  not a full LocalView or latency-certification claim.
- **RuntimeLock**: remains `UNRESOLVED`; this is a capability slice, not a
  runtime resolution.

## Consequences and limits

- The two-world split is now encodable and testable without a fork.
- Order accepted/rejected/canceled responses are not delayed in this slice;
  only fills and funding account changes are. F31/order-ACK latency remains
  out of scope.
- The policy path is isolated by code construction and a static test, not by
  the type system. A future backend-native LocalView would replace the
  adapter queues and make the boundary compiler-enforced.
- Delivery is tick-quantised. A backend timer would reduce the excess latency
  and allow exact `deliver_at` semantics.
- Only one instrument, one strategy and the tested corpus/profile are
  certified. Multi-instrument, L2 queue, impact, liquidation and multi-venue
  behaviour are not claimed.

## Follow-ups

1. Add a backend `deliver_at` timer or event hook if upstream exposes one;
   recertify the first-tick excess distribution.
2. Delay order lifecycle responses (accepted, rejected, canceled, expired)
   and certify F31/F04/F05.
3. Add a multi-instrument and shared-wallet profile.
4. Repeat the 30 s and 90 s response profiles on the 240 s live capture and a
   multi-day corpus.
5. Replace the adapter queues with a backend-native immutable view if one
   becomes available; keep the same evidence schema.

## Reproduction

```sh
cd /home/quant/quant/qel-lv/native/local_view
CARGO_TARGET_DIR=/home/quant/quant/.build-cache/qel-native cargo build --release -j 2

BIN=/home/quant/quant/.build-cache/qel-native/release/qel-local-view
EV=/home/quant/quant/quant-edge-lab/data/native/DS-ARCHIVE-BTCUSDT-2026-11-07/events.jsonl
ST=../../policy/examples/taker_1m.json

$BIN --events $EV --strategy $ST --feed-delay-ns 0          --response-delay-ns 0           --profile control_zero_delay      --out ../../evidence/labd/LOCAL_VIEW_CONTROL_ZERO.json
$BIN --events $EV --strategy $ST --feed-delay-ns 50000000   --response-delay-ns 25000000    --profile f22_main_feed50ms_response25ms --out ../../evidence/labd/LOCAL_VIEW_F22_RUN.json
$BIN --events $EV --strategy $ST --feed-delay-ns 50000000   --response-delay-ns 30000000000 --profile f22_large_response_30s  --out ../../evidence/labd/LOCAL_VIEW_F22_LARGE_RESPONSE_RUN.json
$BIN --events $EV --strategy $ST --feed-delay-ns 50000000   --response-delay-ns 90000000000 --profile f22_decision_in_gap_90s --out ../../evidence/labd/LOCAL_VIEW_F22_DECISION_IN_GAP_RUN.json

/home/quant/quant/.build-cache/qel-native/release/qel-native-runner \
  --events $EV --strategy $ST --out ../../evidence/labd/LOCAL_VIEW_NATIVE_CONTROL.json

cd ../..
PYTHONPATH=python QEL_ROOT=$PWD /home/quant/quant/quant-edge-lab/.venv/bin/python \
  native/local_view/assemble_evidence.py
PYTHONPATH=python QEL_ROOT=$PWD /home/quant/quant/quant-edge-lab/.venv/bin/python \
  -m pytest -q tests/test_local_view.py
(cd specification && sha256sum -c MANIFEST.sha256)
```
