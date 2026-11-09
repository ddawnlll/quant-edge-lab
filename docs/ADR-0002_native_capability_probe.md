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

## Consequences

- No immediate capability or certificate change.
- A bounded Rust spike is the next evidence step for #10/#11 and the liquidation
  portion of #3/#4.
- The acceptance matrix remains authoritative until a functional result exists.
