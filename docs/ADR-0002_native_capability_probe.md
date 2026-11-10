# ADR-0002: Native capability probe and backend decision

Status: PROPOSED, not adopted. This ADR does not change any capability claim.
Date: 2026-11-09.
Base: `f6a850b2026d2910cc0673be5e1a88d9cf7d3a63`.
Probe evidence: `evidence/native/CAPABILITY_PROBE_2026-11-09.json`.
Probe script: `scripts/probe_native_capabilities.py`.
Pinned source: Nautilus `v1.231.0`, commit `27a8e54e7ac3c57d6cbf8891f0283dfbaee97317`.
Candidate source: HftBacktest `v1.8.4`, commit `20cd9470a431e90c526eca6975ef389073c9aca5`, MIT License.

## Context

`locks/CAPABILITY_VERDICT.md` records `CAP-L2-QUEUE`, `CAP-LOCAL-VIEW` and
`CAP-ISOLATED` as `BLOCKED_BACKEND`. A new read-only source probe was run for
issues #3, #4, #10 and #11. The probe checks for named symbols in the pinned
source. It is not a functional test and does not establish a strategy-facing
API. Its purpose is to decide whether a focused functional spike is warranted
before keeping a `BLOCKED_BACKEND` verdict indefinitely.

## Findings

### Pinned Nautilus `v1.231.0`

Present in source:
- liquidation venue configuration, engine step, exchange processing and
  matching-engine forced close (`liquidation_enabled`, `run_venue_liquidations`,
  `process_liquidations`, `liquidate_open_positions`);
- L2 queue position, queue decrement on trade, liquidity consumption and exact
  level quantity (`snapshot_queue_position`, `decrement_queue_on_trade`,
  `apply_liquidity_consumption`, `get_quantity_at_level`);
- shared strategy cache (`data_actor.rs`).

Partial:
- maintenance margin accounting and quote-based liquidation trigger.

Missing:
- an immutable LocalView type;
- a strategy-facing feed-latency/delayed-delivery model;
- evidence that the backtest `SimulatedExchange` exposes the queue and
  liquidation paths through its public configuration.

The presence of matching-engine symbols is significant because the earlier
verdict was based on `SimulatedExchange` having no queue model. It does not
prove that the backtest engine uses those paths or that a strategy can observe
them. A functional spike is required before any claim changes.

### HftBacktest `v1.8.4` (MIT)

Present:
- queue model traits, risk-adverse and probability queue models;
- local processor, feed-latency observation and order latency;
- streaming chunked reader.

Missing:
- own-consumption liquidity budget in the partial-fill exchange;
- an immutable LocalView type;
- margin/liquidation;
- closed-state purge/export.

HftBacktest is a plausible differential adapter for TASK-18 queue and
local-view semantics, but it is not a drop-in replacement for margin,
liquidation or the ledger/evaluator stack.

## Decision proposal

1. Do not adopt a backend change now. Keep `CAP-LOCAL-VIEW` and `CAP-ISOLATED`
   blocked. Do not change `CAP-L2-QUEUE` or the TASK-10 liquidation scope from
   symbol presence alone.
2. Run a focused functional spike in pure Rust against the pinned Nautilus:
   - configure `SimulatedExchange` with the queue/liquidation paths found above;
   - execute F02: ahead 100, qualifying trades 80 then 30, expect exactly 10
     units of fill on the second trade;
   - execute F11/F12: mark-based liquidation with unchanged mark funding debit,
     exact settlement boundary and late response;
   - compare native account postings to the independent accounting oracle.
   Record the exact public API used. If a hook is unreachable, record the
   failing call and keep `BLOCKED_BACKEND`.
3. If the functional spike passes, open a recertification ADR for TASK-08,
   TASK-10, TASK-17 and any dependent certificates. Re-run the affected
   fixtures, B2/B3/B4, and update the capability verdict. LocalView still
   requires a separate delayed-delivery design.
4. If the functional spike fails, retain the blocked verdict and attach the
   exact failing symbol/call to `CAP-L2-QUEUE` and `CAP-ISOLATED`.
5. Evaluate HftBacktest only as a differential adapter for TASK-18, never as
   the production execution or accounting stack. Its queue/local-view evidence
   must be compared on shared semantics, with timestamp, cancel and queue
   assumptions recorded.

## Functional result (2026-11-09)

Evidence: `evidence/native/FUNCTIONAL_SPIKE_2026-11-09.json`.
Spike crate: `native/capability_spike` (pure Rust, same pinned rev
`27a8e54`). This section records observed behavior only; it does not adopt a
backend change.

Commands (exit code 0):

```sh
cd native/capability_spike
CARGO_TARGET_DIR=/home/quant/quant/.build-cache/qel-native cargo build --release -j 4
/home/quant/quant/.build-cache/qel-native/release/qel-capability-spike \
  /tmp/qel_functional_spike.json
```

Build 20 s, spike run 90 ms, peak RSS 16.4 MiB. Binary 12,373,384 bytes.

### Per-hook result

| Hook | Result | Exact API / observed |
| --- | --- | --- |
| `snapshot_queue_position` | PASS (behavior) | `SimulatedVenueConfig::builder().queue_position(true)` |
| `decrement_queue_on_trade` | PASS (behavior) | same config + `TradeTick`; F02 and depth-once both exact |
| `apply_liquidity_consumption` | PARTIAL | `liquidity_consumption(true)`; maker queue path exercised, marketable-order budget not separately asserted |
| `get_quantity_at_level` | PASS (public) | `OrderBook::get_quantity_at_level(Price, OrderSide, u8) -> Quantity` returned 100.0 for a 100.0 bid level |
| `run_venue_liquidations` | PASS (behavior) | `liquidation_enabled(true)` + engine `run`; symbol is a private method |
| `process_liquidations` | PASS (behavior) | `SimulatedExchange::process_liquidations` is `pub` but unreachable; `venues` is private |
| `liquidate_open_positions` | PASS (behavior) | `OrderMatchingEngine::liquidate_open_positions` is `pub` but unreachable; `matching_engines` is private |
| mark-based maintenance | FAIL | native maintenance is entry-based: 5.0, oracle expects 4.7 |
| quote/mark split | FAIL | `Cache::calculate_unrealized_pnl` uses `quote.bid_price`; a quote-only move to 94 triggers the close |
| isolated margin account | FAIL | `AccountType::Margin` is one balance per currency; no isolated sub-account |
| liquidation fee | FAIL | no field; forced close is a normal market order |
| native account postings | FAIL (not exposed) | no journal/posting API; `BacktestResult` has aggregate stats only |

Queue state (`queue_ahead_total`, `queue_pending`, `queue_excess`) is private
and has no public readback. F02 ahead=0 is inferred from the exact fill.

### F02 queue-ahead

Setup from `specification/fixtures/conformance.json`: resting BUY 20 @ 100,
ahead 100, qualifying seller trades 80 then 30.

- after trade 80: `filled_qty = 0.0`
- after trade 30: `fill_step_2_qty = 10.0`, `leaves_qty = 10.0`
- L2 depth-once variant: trade 80 then a depth update 100 -> 20, then trade 30:
  `fill_step_2_qty = 10.0`; the depth reduction did not double-consume
- `get_quantity_at_level` direct call: 100.0

### F11/F12 liquidation

F11 (cash 10, qty 1, entry 100, mark 94, quote bid 94):

- native equity 4.0, maintenance 5.0, one `LIQUIDATION-*` market order,
  position flat, final cash 4.0
- oracle: equity 4, maintenance 4.7, triggered, cost 0.09, final cash 3.91
- trigger and equity match; maintenance differs by +0.3 (entry-based) and
  final cash by +0.09 (no liquidation fee)

F11 `QUOTE_IS_NOT_MARK` (no `MarkPriceUpdate`, only quote 94): native still
closes the position. This scenario FAILS.

F12 (cash 5.1, qty 1, entry 100, mark 100, funding rate 0.002 at the exact
boundary):

- native funding adjustment -0.2, equity 4.9, maintenance 5.0, one
  `LIQUIDATION-*` order, position flat, final cash 4.9
- oracle: funding -0.2, equity 4.9, maintenance 5.0, triggered, cost 0.1,
  final cash 4.8
- all metrics match because mark == entry; final cash differs by +0.10 (no
  liquidation fee)
- late response (boundary 2, funding update at 3): `engine.run` returns
  `Late funding boundary ... 1000000002 < replay timestamp 1000000003` and
  applies no funding adjustment

### Postings comparison

Nautilus exposes no journal or double-entry postings. Only account balances,
positions and orders are readable from `Cache`. The independent Python exact
accounting oracle therefore remains the ledger for comparison; no native
posting comparison was possible.

### Recommendation

1. Queue: F02 passes functionally. The coordinator may open a narrow
   recertification ADR for `CAP-L2-QUEUE` limited to queue-ahead, trade
   decrement and depth-once consumption. The queue state is not publicly
   readable and the model is per-order and trade-driven.
2. Liquidation: retain `BLOCKED_BACKEND` for `CAP-ISOLATED` and the TASK-10
   mark-based liquidation scope. The forced close fires, but the trigger and
   maintenance semantics do not match the mark-based specification.
3. `CAP-LOCAL-VIEW` is unchanged and was not tested.
4. Do not treat native state as a posting ledger; keep the Python oracle.

No lock, certificate, task file or production module was changed by this
spike. ADR status stays PROPOSED.

## Consequences

- No immediate capability or certificate change.
- A bounded Rust spike is the next evidence step for #10/#11 and the liquidation
  portion of #3/#4.
- The acceptance matrix remains authoritative until a functional result exists.
