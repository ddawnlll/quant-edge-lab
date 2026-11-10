# ADR-0003: Narrow L2 queue recertification after functional F02

Status: ACCEPTED for the narrow F02 queue-ahead slice.
Date: 2026-11-10.
Base: `d6fea497229626e26011edc45c2a6bf9c288c85e`.
Evidence: `evidence/native/FUNCTIONAL_SPIKE_2026-11-09.json`.
Spike: `native/capability_spike/`.
Pinned source: Nautilus `v1.231.0`, commit `27a8e54e7ac3c57d6cbf8891f0283dfbaee97317`.

## Context

`locks/CAPABILITY_VERDICT.md` previously recorded `CAP-L2-QUEUE` as
`BLOCKED_BACKEND` because `SimulatedExchange` was believed to have no
per-price queue-ahead model. A source probe (ADR-0002) found queue symbols in
the matching engine, and a pure-Rust functional spike has now exercised them
through the public venue configuration.

## Functional result

The following are observed, not inferred:

- `SimulatedVenueConfig::builder().queue_position(true).liquidity_consumption(true).trade_execution(true)`
  reaches the queue model.
- F02: ahead 100, qualifying trades 80 then 30. The maker order has
  `filled_qty = 0` after trade 1 and exactly `filled_qty = 10` after trade 2,
  with `leaves_qty = 10`. This matches the hand oracle.
- A trade plus the corresponding depth reduction does not double-consume
  volume.
- `OrderBook::get_quantity_at_level(Price, OrderSide, u8) -> Quantity` is
  public and returns the expected level quantity.

The following remain unsupported or unproven:

- queue state is a private `OrderMatchingEngine` field with no public readback;
  the ahead quantity is inferred from fills, not observed directly;
- the own-consumption budget is exercised only for the maker queue path; a
  marketable-order budget is not separately asserted;
- shared-level budget across multiple orders, unknown-aggressor handling and
  resync after gaps are not certified;
- full L2 depth semantics beyond the tested level are not certified.

## Decision

1. `CAP-L2-QUEUE` changes from `BLOCKED_BACKEND` to **PARTIAL** for the
   F02 queue-ahead slice only.
2. TASK-17 may implement and test this slice: passive maker orders with
   `queue_position(true)`, trade-driven queue depletion, partial maker fills,
   and the depth-once rule. Every claim must be scoped to the tested fixtures.
3. TASK-18 remains `BLOCKED_BACKEND` because `CAP-LOCAL-VIEW` is still missing:
   there is no immutable LocalView or delayed-delivery API.
4. TASK-10 mark-based liquidation and `CAP-ISOLATED` remain
   `BLOCKED_BACKEND`. The spike found that maintenance is entry-based
   (5.0 observed vs 4.7 oracle), a quote-only move triggers liquidation, there
   is no isolated sub-account and no liquidation fee, and native postings are
   not exposed.
5. `RuntimeLock.status` remains `UNRESOLVED` because the full hook set is not
   available. The recertification is recorded as a capability slice, not a
   runtime resolution.
6. Rerun F02 and the affected TASK-17 fixtures after any adapter or pin change.
   A future full L2 claim requires shared-level, unknown-aggressor and resync
   evidence.

## Consequences

- Queue-ahead work on TASK-17 is unblocked for the F02 slice.
- The two-world/LocalView work remains blocked.
- The taker L1 funding certificate is unchanged.
- No production module was changed by this ADR; the spike is evidence only.
