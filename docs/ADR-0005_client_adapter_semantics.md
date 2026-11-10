# ADR-0005: Client adapter semantics over the pinned native matching engine

Status: PROPOSED.
Date: 2026-11-10.
Task: TASK-17 / issue #10.
Base: `ce91981055125328dae4b59add8e5432892c4f20`.
Branch: `qel/w-passive-completion`.
Profile: `ADAPTER_OVER_NATIVE`.
Pinned source: Nautilus `v1.231.0`, commit
`27a8e54e7ac3c57d6cbf8891f0283dfbaee97317`.
Evidence: `evidence/labd/PASSIVE_QUEUE_ADAPTER_2026-11-10.json`.
Code: `native/passive_runner/src/adapter.rs`,
`native/passive_runner/src/main.rs`.
Tests: `tests/test_passive_queue_adapter.py`.

## Context

ADR-0003 certified `CAP-L2-QUEUE` as PARTIAL for the F02 queue-ahead slice and
recorded that F04, F05, F07, F29, F31 and the client-managed OCO profile had no
reachable native hook:

| Fixture | Native gap previously recorded |
| --- | --- |
| F04 | `OrderMatchingEngine::process_cancel` purges a terminal order silently; no `OrderCancelRejected`/`ALREADY_FILLED` response |
| F05 | no public `priority_reset_count` or queue-ahead readback; `process_modify` only re-snapshots the queue on a price change |
| F07 | `nautilus_model::position::Position` has no epoch field; no stale-entity rejection path or error code |
| F29 | `OrderBook` exposes `sequence` as a high-water mark only; no snapshot anchor, `last_good_seq`, gap validation or INVALID state |
| F31 | `OrderAccepted`/`OrderFilled` carry no venue sequence; no `LOCAL_ORDER_EVENT` injection hook; native `leaves_qty` is not zeroed on cancel |
| F06 client | no client-order group/epoch advisory and no both-legs-eligible event for a plain client pair |

Those gaps are real and remain in the pinned dependency. What was missing was
not a venue hook but a client layer: a trading system that uses Nautilus as the
exchange still owns order state, kill switches, sequencing, epochs and risk
gating. This ADR proposes to implement exactly that layer as an adapter, not as
a fork or monkey patch, and to test it against the same hand oracle.

## Decision (proposed)

1. Introduce `native/passive_runner/src/adapter.rs`: a client order-management
   adapter with profile `ADAPTER_OVER_NATIVE`.
2. The adapter never matches orders. Every submission, cancel, modify and fill
   in each probe is also executed by the pinned `BacktestEngine`; native
   matching remains the exchange of record.
3. The adapter owns only client facts that the public venue API does not
   expose: local order state, client order ids, cancel responses, replace
   policy, position epochs, book-sequence validation, venue sequence stamping,
   duplicate/out-of-order event suppression, client OCO groups and reduce-only
   sizing.
4. Every adapter probe must record (a) the adapter behavior, (b) the observable
   native behavior for the same scenario, and (c) the exact native limitation.
   A verdict is PASS only when the adapter field matches the oracle and the
   native limitation is recorded; the native verdict for the same fixture is
   not upgraded.
5. The adapter is not a capability claim on the venue. `CAP-L2-QUEUE` remains
   PARTIAL. No native probe verdict is weakened: the F02/F03/F06/F33/F34 probes
   are byte-identical to `PASSIVE_QUEUE_COMPLETION_2026-11-10.json` (modulo
   timing), and F29 stays `BLOCKED_BACKEND` natively.

## Adapter-managed semantics

### F04 cancel race: explicit `ALREADY_FILLED`

- The adapter dispatches the venue cancel at `t=10`; with a 20 ns
  `StaticLatencyModel` delete latency it arrives at `t=30`.
- The order fills at `t=20` through the native matching engine. The adapter
  records the fill and moves its local order to `FILLED`.
- On arrival the adapter resolves the pending cancel against local state and
  returns `ALREADY_FILLED` without touching a terminal order again.
- No quantity is overfilled: `filled + canceled <= quantity`.

Native boundary: the venue still purges the late cancel silently. The
`ALREADY_FILLED` response is a client fact, produced at the client's own
arrival boundary.

### F05 replace: cancel+new on price-priority reset

- The increase `10 -> 20` at the same price is a client-managed cancel+new. The
  remainder (8) is canceled and a successor order is submitted at the back of
  the queue. `priority_reset_count = 1` and `ahead_qty = 100` (the displayed
  quantity at the level) are adapter fields.
- The decrease `20 -> 15` keeps priority and is an in-place resize of the same
  client order.
- Client aggregate: `filled 2`, `leaves 13`, `accepted 15`.
- The native control in the same evidence run still shows
  `preserved_on_same_price_increase`: a native same-price quantity change does
  not reset the tracked queue.

Native boundary: no public `priority_reset_count` and no public queue-ahead
readback; `OrderMatchingEngine.queue_ahead_total` and
`snapshot_queue_position` remain private. The adapter's priority policy is the
client's, and the native divergence is recorded, not hidden.

### F07 epoch safety: stale entity rejected before the venue

- The adapter stamps a monotonic position epoch when a position opens
  (including a reopen after flat): epoch 1 on the first open, epoch 2 after the
  close/reopen.
- A stop built for epoch 1 while the position is at epoch 2 is rejected with
  `STALE_POSITION_EPOCH` inside the adapter. No order is registered and no
  venue command is issued; `stale_fill_count = 0`.
- A stop built for the current epoch is accepted and submitted; the native
  venue accepts it.
- The native control submits the same epoch-1 stop straight to the venue: it
  is `Accepted`, proving there is no native epoch gate.

Native boundary: `Position` has no epoch field and no stale-entity error code
exists under the pinned source.

### F29 sequence safety: INVALID/RESYNC_REQUIRED with `last_good_seq`

- The adapter installs a snapshot anchor (`last_update_id = 100`) and accepts a
  delta only when its `[first, last]` range covers the anchor or its
  `previous` links to the anchor.
- A delta whose range does not chain (`[103,103]` with `previous = 999`) puts
  the adapter book in `INVALID`, retains `last_good_seq = 102` and blocks new
  risk with `BOOK_RESYNC_REQUIRED`.
- The rejected delta is never forwarded to `OrderBook::apply_delta`: the native
  book keeps `sequence = 102`, no `99.00` level appears and the level sizes are
  unchanged.
- A fresh snapshot (`last_update_id = 110`) returns the adapter to `VALID` and
  a subsequent contiguous delta is applied again.
- Absolute size updates replace the level quantity (100 -> 60, not 160).

Native boundary: `OrderBook` has no `last_good_seq`, no book state and no gap
validation. The guard is client state wrapped around the public book.

### F31 event sequencing: dedupe, ordering and canceled leaves

- The adapter stamps a venue sequence on every order/fill event it accepts,
  because the pinned engine carries none.
- A duplicate fill keyed by (order id, venue sequence, trade id) is suppressed;
  no second strategy-visible fill and no fee debit is produced.
- A delayed ACK with a lower venue sequence is suppressed by state
  monotonicity: the local order never regresses from `PARTIALLY_FILLED`.
- On cancel the adapter sets local leaves to zero and moves canceled quantity
  into `canceled_qty`: `accepted 2`, `filled 1`, `leaves 0`, `canceled 1`,
  `state CANCELED`.
- The native order is recorded with `leaves_qty = 1` after the same cancel,
  which is the fixture's documented native mismatch.

Native boundary: no venue sequence on order events, no `LOCAL_ORDER_EVENT`
injection operation, and native leaves are not zeroed on cancel.

### F06 client OCO profile: atomic sibling cancel, no overfill

- The client bracket is sized from the filled entry quantity only (1 lot).
- Both exits are plain venue orders in client group `GR-1` with `reduce_only`.
- On the first group fill the adapter cancels the surviving sibling before the
  next price move (`ATOMIC_CLIENT_OCO`), and the sibling is `Canceled` at the
  venue.
- A second exit request while flat is rejected locally with
  `REDUCE_ONLY_REJECT` and never submitted. The native control order in the
  same run is also `Rejected`, so both layers agree while the native OCO path
  is unchanged.
- Result: one exit fill for a one-lot entry, `position_qty = 0`, no overfill,
  no position reversal.

Native boundary: the native OCO contingency group is only atomic for a bracket
submitted as an order list. A client-managed pair has no group state, no
group-level cancel and no both-legs-eligible event.

### F02 unknown aggressor: conservative queue

- A trade whose aggressor is unknown does not deplete the adapter's queue-ahead
  and never produces an adapter-credited fill; the known-aggressor path does.
- The pinned native engine treats `AggressorSide::NoAggressor` as a two-sided
  trade (it decrements queue-ahead and forces both sides to the trade price),
  so it filled 10 lots where the conservative client model filled 0. This
  profile difference is recorded in the evidence and the probe is PARTIAL.

Native boundary: the client model intentionally does not reproduce a two-sided
unknown print, because the print cannot be attributed to the resting side.

## Native API boundary (public only)

The adapter uses no private symbol and no monkey patch. The native side of each
probe uses:

- `SimulatedVenueConfig::builder().queue_position(..).liquidity_consumption(..)
  .trade_execution(true).support_contingent_orders(true).use_reduce_only(true)
  .reject_stop_orders(true).maybe_latency_model(..)`
- `StaticLatencyModel::new(base, insert, update, delete)` for the F04 delayed
  cancel arrival
- `Strategy::submit_order`, `submit_order_list`, `cancel_order`,
  `modify_order`
- `Cache::order(ClientOrderId)` and `Order::filled_qty/leaves_qty/status/
  quantity/avg_px`
- `Cache::positions_open/positions_closed`
- `BacktestEngine::kernel().cache` and `kernel_mut().exec_engine` only for the
  existing F31 duplicate-fill injection probe
- `OrderBook::new`, `OrderBook::apply_delta`,
  `OrderBook::get_quantity_at_level`, `OrderBook::sequence`

Not used: `OrderMatchingEngine::snapshot_queue_position`,
`decrement_queue_on_trade`, `queue_ahead_total`, `queue_pending`,
`queue_ahead_orders`, `queue_excess`, `priority_reset_count`, or any
`Position` epoch field (none exists).

## Recertification impact

- TASK-17: the adapter slice is functionally tested for F04, F05, F06 client
  profile, F07, F29 and F31 against the hand oracle. The capability verdict is
  not upgraded: `CAP-L2-QUEUE` remains PARTIAL, because the native venue still
  lacks these semantics and the adapter is a client layer.
- TASK-18: still `BLOCKED_BACKEND`. This ADR does not provide an immutable
  LocalView, a delayed local delivery path or a two-world split; the adapter is
  not a substitute for `CAP-LOCAL-VIEW`.
- TASK-10 mark-based liquidation and `CAP-ISOLATED` remain `BLOCKED_BACKEND`.
- `RuntimeLock.status` remains `UNRESOLVED`.
- Recertification of the F02 slice is unaffected. If the pin, the adapter or
  the probe wiring changes, rerun `tests/test_passive_queue.py`,
  `tests/test_passive_queue_adapter.py` and the runner.

## What is still not claimed

- Any adapter behavior as a native venue capability.
- Native queue-state readback, native `priority_reset_count`, native position
  epochs, native book invalidation/resync or native venue sequencing.
- Order-book matching fidelity from the adapter; the adapter does not match.
- Persistence or restart durability of adapter state; it is in-process only.
- Resync from a real venue snapshot feed; the F29 snapshot is a client input.
- Unknown-aggressor fill parity with the pinned native engine.
- Shared-level budget, full L2 depth, unknown-aggressor and resync behavior as
  native venue properties for the F02 certification.
- `CAP-LOCAL-VIEW`, `CAP-ISOLATED`, mark-based liquidation.

## Consequences

- The previously blocked fixture behaviors become testable and are tested at
  the layer where they actually live, without weakening or forking the pinned
  engine.
- Evidence consumers must read the profile label. An `ADAPTER_OVER_NATIVE`
  PASS is not a Nautilus PASS.
- Adapter state is deliberately small and in-process; a production OMS would
  need durable state, replay and reconciliation, which are out of scope here.
- `evidence/labd/PASSIVE_QUEUE_COMPLETION_2026-11-10.json` is not edited. Two
  artifact-hash assertions in `tests/test_passive_queue.py`
  (`test_evidence_records_binary_and_source_hashes` and
  `test_binary_hash_matches_built_artifact_if_present`) pin that audited source
  and binary. Both changed with this slice, so those two assertions are stale
  by construction and need re-pinning in a follow-up; every behavioral
  assertion in that module still passes with this build.
