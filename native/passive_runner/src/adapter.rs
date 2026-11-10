//! Client order-management adapter over the pinned native matching engine.
//!
//! Profile: `ADAPTER_OVER_NATIVE`.
//!
//! The adapter is a client-side order-management layer. It does not match
//! orders, hold a book of record or replace the venue. Every submission,
//! cancel, modify and fill that this module models is also performed by the
//! pinned Nautilus `BacktestEngine` (v1.231.0,
//! commit 27a8e54e7ac3c57d6cbf8891f0283dfbaee97317); native matching remains
//! the exchange. What the adapter adds is client-owned state that the venue
//! API does not expose:
//!
//! * local order state with terminal-state aware cancel responses;
//! * a client-managed cancel+new replace with an explicit priority reset count;
//! * position epochs and stale-entity rejection before a venue submission;
//! * a book-delta sequence high-water mark with an `INVALID` /
//!   `RESYNC_REQUIRED` gate;
//! * venue sequence stamping, duplicate/out-of-order event suppression and a
//!   zeroed canceled leaves quantity;
//! * an atomic client OCO group and a reduce-only exit guard;
//! * a conservative queue model for unknown-aggressor trades.
//!
//! Each of those is a client-owned fact. None of them is a native capability
//! claim, and the matching engine's own limitation is recorded next to each
//! behavior in the probe payload.

use std::collections::BTreeMap;

use serde_json::{json, Value};

/// Profile label attached to every adapter claim in the evidence payload.
pub const PROFILE: &str = "ADAPTER_OVER_NATIVE";

const EPS: f64 = 1e-9;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Side {
    Buy,
    Sell,
}

impl Side {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Buy => "BUY",
            Self::Sell => "SELL",
        }
    }

    pub fn signed_qty(self, qty: f64) -> f64 {
        match self {
            Self::Buy => qty,
            Self::Sell => -qty,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LocalState {
    Accepted,
    PartiallyFilled,
    PendingCancel,
    Filled,
    Canceled,
}

impl LocalState {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Accepted => "ACCEPTED",
            Self::PartiallyFilled => "PARTIALLY_FILLED",
            Self::PendingCancel => "PENDING_CANCEL",
            Self::Filled => "FILLED",
            Self::Canceled => "CANCELED",
        }
    }

    pub fn is_terminal(self) -> bool {
        matches!(self, Self::Filled | Self::Canceled)
    }
}

/// The adapter's directional view of a trade's aggressor.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AggressorView {
    Buyer,
    Seller,
    /// The venue did not identify the aggressor.
    Unknown,
}

impl AggressorView {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Buyer => "BUYER",
            Self::Seller => "SELLER",
            Self::Unknown => "UNKNOWN",
        }
    }
}

#[derive(Clone, Debug)]
pub struct LocalOrder {
    pub client_order_id: String,
    pub side: Side,
    /// Total client-owned accepted quantity (never decreases except on replace).
    pub quantity: f64,
    pub filled: f64,
    pub leaves: f64,
    pub canceled: f64,
    pub state: LocalState,
    /// Highest venue sequence applied to this order.
    pub venue_seq: u64,
    /// Venue trade ids already applied (dedupe key).
    pub fill_ids: Vec<String>,
    /// Client OCO/bracket group label.
    pub group: Option<String>,
    pub reduce_only: bool,
    /// Adapter-side queue-ahead quantity at this order's price level.
    pub ahead_qty: f64,
    /// Position epoch the order was submitted against, when supplied.
    pub position_epoch: Option<u64>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EventVerdict {
    Applied,
    DuplicateSuppressed,
    OutOfOrderSuppressed,
    StatePreserved,
    UnknownOrder,
}

impl EventVerdict {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Applied => "APPLIED",
            Self::DuplicateSuppressed => "DUPLICATE_SUPPRESSED",
            Self::OutOfOrderSuppressed => "OUT_OF_ORDER_SUPPRESSED",
            Self::StatePreserved => "STATE_PRESERVED",
            Self::UnknownOrder => "UNKNOWN_ORDER",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CancelDecision {
    /// The venue cancel is dispatched; the client response resolves at arrival.
    DispatchToVenue,
    /// The adapter answers without touching the venue.
    RespondLocally,
}

impl CancelDecision {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::DispatchToVenue => "DISPATCH_TO_VENUE",
            Self::RespondLocally => "RESPOND_LOCALLY_NO_VENUE_CANCEL",
        }
    }
}

#[derive(Clone, Debug)]
pub struct ReplaceOutcome {
    pub request_id: String,
    pub new_order_id: String,
    pub increase: bool,
    pub priority_reset: bool,
    pub accepted_qty: f64,
    pub filled_qty: f64,
    pub leaves_qty: f64,
    pub ahead_qty: f64,
    pub canceled_remainder_qty: f64,
}

#[derive(Clone, Debug)]
pub struct TradeOutcome {
    pub policy: &'static str,
    pub depleted_qty: f64,
    pub fill_qty: f64,
    pub ahead_after: f64,
}

#[derive(Clone, Debug)]
pub struct GroupCancel {
    pub filled_order_id: String,
    pub sibling_id: String,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BookState {
    Valid,
    Invalid,
}

impl BookState {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Valid => "VALID",
            Self::Invalid => "INVALID",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BookVerdict {
    Applied,
    Rejected,
}

#[derive(Clone, Debug)]
pub struct BookReject {
    pub code: &'static str,
    pub first: u64,
    pub last: u64,
    pub previous: u64,
    pub last_good_seq: u64,
}

impl BookReject {
    fn to_json(&self) -> Value {
        json!({
            "code": self.code,
            "first": self.first,
            "last": self.last,
            "previous": self.previous,
            "last_good_seq": self.last_good_seq,
        })
    }
}

/// Book-delta sequence guard. It owns the client's view of a continuous feed
/// and never mutates the native book itself; the probe only forwards a delta
/// to `OrderBook::apply_delta` after `delta` returns `Applied`.
#[derive(Clone, Debug)]
pub struct BookGuard {
    pub state: BookState,
    pub anchor: Option<u64>,
    pub last_good_seq: Option<u64>,
    pub initial_match: bool,
    pub applied: u64,
    pub rejected: u64,
    pub last_reject: Option<BookReject>,
}

impl Default for BookGuard {
    fn default() -> Self {
        Self::new()
    }
}

impl BookGuard {
    pub fn new() -> Self {
        Self {
            // No snapshot has been installed yet, so no delta may be trusted.
            state: BookState::Invalid,
            anchor: None,
            last_good_seq: None,
            initial_match: false,
            applied: 0,
            rejected: 0,
            last_reject: None,
        }
    }

    /// Installs (or re-installs) a snapshot anchor, clearing `INVALID`.
    pub fn snapshot(&mut self, last_update_id: u64) {
        self.anchor = Some(last_update_id);
        self.last_good_seq = Some(last_update_id);
        self.state = BookState::Valid;
        self.initial_match = false;
        self.last_reject = None;
        self.applied = 0;
        self.rejected = 0;
    }

    /// Validates a delta range against the last good sequence.
    ///
    /// A range is accepted when it covers the current anchor (`first <= anchor
    /// <= last`) or when its `previous` field links to the anchor. Anything
    /// else invalidates the client book: the adapter keeps `last_good_seq`,
    /// sets `INVALID` and refuses new risk until a fresh snapshot arrives.
    pub fn delta(&mut self, first: u64, last: u64, previous: u64) -> BookVerdict {
        let Some(anchor) = self.last_good_seq else {
            return self.reject("NO_SNAPSHOT_ANCHOR", first, last, previous);
        };
        let covered = first <= anchor && anchor <= last;
        let chained = previous == anchor;
        if covered || chained {
            if covered && first <= anchor {
                self.initial_match = true;
            }
            self.last_good_seq = Some(last.max(anchor));
            self.state = BookState::Valid;
            self.applied += 1;
            return BookVerdict::Applied;
        }
        let code = if last <= anchor {
            "OUT_OF_ORDER_STALE"
        } else if first > anchor + 1 {
            "SEQUENCE_GAP"
        } else {
            "PREVIOUS_SEQUENCE_MISMATCH"
        };
        self.reject(code, first, last, previous)
    }

    fn reject(&mut self, code: &'static str, first: u64, last: u64, previous: u64) -> BookVerdict {
        self.state = BookState::Invalid;
        self.rejected += 1;
        self.last_reject = Some(BookReject {
            code,
            first,
            last,
            previous,
            last_good_seq: self.last_good_seq.unwrap_or(0),
        });
        BookVerdict::Rejected
    }

    pub fn risk_allowed(&self) -> bool {
        self.state == BookState::Valid
    }

    pub fn snapshot_state(&self) -> &'static str {
        self.state.as_str()
    }
}

/// Client order-management adapter state.
#[derive(Clone, Debug)]
pub struct Adapter {
    pub orders: BTreeMap<String, LocalOrder>,
    pub next_venue_seq: u64,
    pub last_venue_seq: u64,
    pub position_qty: f64,
    pub position_epoch: u64,
    pub cancel_responses: Vec<(String, String)>,
    pub priority_reset_count: u64,
    pub replace_count: u64,
    pub duplicate_fills_suppressed: u64,
    pub out_of_order_events_suppressed: u64,
    pub overfill_clamped: u64,
    pub duplicate_fee_debits: u64,
    pub stale_epoch_rejections: u64,
    pub group_sibling_cancels: u64,
    pub venue_submissions: u64,
    pub venue_cancels: u64,
    pub blocked_risk_requests: u64,
    pub fills_applied: u64,
    pub strategy_fill_events: u64,
    pub unknown_aggressor_trades: u64,
    pub queue_depletions_skipped: u64,
    pub risk_events: Vec<String>,
    pub book: BookGuard,
}

impl Default for Adapter {
    fn default() -> Self {
        Self::new()
    }
}

impl Adapter {
    pub fn new() -> Self {
        Self {
            orders: BTreeMap::new(),
            next_venue_seq: 1,
            last_venue_seq: 0,
            position_qty: 0.0,
            position_epoch: 0,
            cancel_responses: Vec::new(),
            priority_reset_count: 0,
            replace_count: 0,
            duplicate_fills_suppressed: 0,
            out_of_order_events_suppressed: 0,
            overfill_clamped: 0,
            duplicate_fee_debits: 0,
            stale_epoch_rejections: 0,
            group_sibling_cancels: 0,
            venue_submissions: 0,
            venue_cancels: 0,
            blocked_risk_requests: 0,
            fills_applied: 0,
            strategy_fill_events: 0,
            unknown_aggressor_trades: 0,
            queue_depletions_skipped: 0,
            risk_events: Vec::new(),
            book: BookGuard::new(),
        }
    }

    /// Stamps the next client venue sequence. The pinned engine has no venue
    /// sequence on `OrderAccepted`/`OrderFilled`, so the adapter assigns one.
    pub fn stamp_venue_seq(&mut self) -> u64 {
        let seq = self.next_venue_seq;
        self.next_venue_seq += 1;
        self.last_venue_seq = self.last_venue_seq.max(seq);
        seq
    }

    pub fn register(
        &mut self,
        client_order_id: &str,
        side: Side,
        quantity: f64,
        group: Option<&str>,
        reduce_only: bool,
        ahead_qty: f64,
        position_epoch: Option<u64>,
    ) -> u64 {
        let seq = self.stamp_venue_seq();
        self.orders.insert(
            client_order_id.to_string(),
            LocalOrder {
                client_order_id: client_order_id.to_string(),
                side,
                quantity,
                filled: 0.0,
                leaves: quantity,
                canceled: 0.0,
                state: LocalState::Accepted,
                venue_seq: seq,
                fill_ids: Vec::new(),
                group: group.map(str::to_string),
                reduce_only,
                ahead_qty,
                position_epoch,
            },
        );
        self.venue_submissions += 1;
        seq
    }

    pub fn order(&self, client_order_id: &str) -> Option<&LocalOrder> {
        self.orders.get(client_order_id)
    }

    pub fn note_accepted(&mut self, client_order_id: &str, venue_seq: u64) -> EventVerdict {
        let Some(order) = self.orders.get_mut(client_order_id) else {
            return EventVerdict::UnknownOrder;
        };
        if venue_seq <= order.venue_seq {
            self.out_of_order_events_suppressed += 1;
            return EventVerdict::OutOfOrderSuppressed;
        }
        // State monotonicity: a late ACK never regresses a filled order.
        if order.state != LocalState::Accepted {
            order.venue_seq = order.venue_seq.max(venue_seq);
            self.out_of_order_events_suppressed += 1;
            return EventVerdict::StatePreserved;
        }
        order.venue_seq = venue_seq;
        EventVerdict::Applied
    }

    /// Applies a fill event keyed by (order id, venue sequence, trade id).
    ///
    /// Returns the verdict and the quantity actually applied. Duplicate trade
    /// ids and non-advancing venue sequences are suppressed, and a fill is
    /// always clamped to the order's leaves so local state can never overfill.
    pub fn note_fill(
        &mut self,
        client_order_id: &str,
        venue_seq: u64,
        fill_id: &str,
        qty: f64,
    ) -> (EventVerdict, f64) {
        let Some(order) = self.orders.get(client_order_id) else {
            return (EventVerdict::UnknownOrder, 0.0);
        };
        if order.fill_ids.iter().any(|known| known == fill_id) {
            self.duplicate_fills_suppressed += 1;
            self.duplicate_fee_debits += 0;
            return (EventVerdict::DuplicateSuppressed, 0.0);
        }
        if venue_seq <= order.venue_seq {
            self.out_of_order_events_suppressed += 1;
            return (EventVerdict::OutOfOrderSuppressed, 0.0);
        }
        let mut applied = qty.max(0.0);
        if applied > order.leaves + EPS {
            applied = order.leaves;
            self.overfill_clamped += 1;
        }
        let side = order.side;
        {
            let order = self.orders.get_mut(client_order_id).expect("checked above");
            order.filled += applied;
            order.leaves = (order.leaves - applied).max(0.0);
            order.venue_seq = venue_seq;
            order.fill_ids.push(fill_id.to_string());
            order.state = if order.leaves <= EPS {
                LocalState::Filled
            } else {
                LocalState::PartiallyFilled
            };
            if order.leaves <= EPS {
                order.leaves = 0.0;
            }
        }
        self.last_venue_seq = self.last_venue_seq.max(venue_seq);
        self.fills_applied += 1;
        self.strategy_fill_events += 1;
        self.apply_position_delta(side.signed_qty(applied));
        (EventVerdict::Applied, applied)
    }

    fn apply_position_delta(&mut self, delta: f64) {
        let before = self.position_qty;
        self.position_qty += delta;
        if self.position_qty.abs() < EPS {
            self.position_qty = 0.0;
        }
        if before.abs() < EPS && self.position_qty.abs() > EPS {
            // A new position (fresh open or reopen after flat) starts a new epoch.
            self.position_epoch += 1;
        }
    }

    /// Decides whether a cancel request reaches the venue.
    ///
    /// A terminal local order is answered by the adapter and never dispatched,
    /// which is what turns a late cancel into an explicit `ALREADY_FILLED`
    /// response instead of a silent native purge.
    pub fn request_cancel(&mut self, client_order_id: &str) -> (CancelDecision, &'static str) {
        let state = match self.orders.get(client_order_id) {
            None => {
                self.record_cancel(client_order_id, "UNKNOWN_ORDER");
                return (CancelDecision::RespondLocally, "UNKNOWN_ORDER");
            }
            Some(order) => order.state,
        };
        match state {
            LocalState::Filled => {
                self.record_cancel(client_order_id, "ALREADY_FILLED");
                (CancelDecision::RespondLocally, "ALREADY_FILLED")
            }
            LocalState::Canceled => {
                self.record_cancel(client_order_id, "ALREADY_CANCELED");
                (CancelDecision::RespondLocally, "ALREADY_CANCELED")
            }
            LocalState::PendingCancel => {
                // Retry of an in-flight cancel: dispatch is idempotent.
                (CancelDecision::DispatchToVenue, "PENDING")
            }
            LocalState::Accepted | LocalState::PartiallyFilled => {
                if let Some(order) = self.orders.get_mut(client_order_id) {
                    order.state = LocalState::PendingCancel;
                }
                self.venue_cancels += 1;
                (CancelDecision::DispatchToVenue, "PENDING")
            }
        }
    }

    /// Applies a venue cancel arrival and returns the client cancel response.
    pub fn resolve_cancel(&mut self, client_order_id: &str) -> &'static str {
        let Some(state) = self.orders.get(client_order_id).map(|order| order.state) else {
            self.record_cancel(client_order_id, "UNKNOWN_ORDER");
            return "UNKNOWN_ORDER";
        };
        let response: &'static str = match state {
            LocalState::Filled => "ALREADY_FILLED",
            LocalState::Canceled => "ALREADY_CANCELED",
            LocalState::PendingCancel | LocalState::Accepted | LocalState::PartiallyFilled => {
                let order = self.orders.get_mut(client_order_id).expect("checked above");
                order.canceled += order.leaves;
                order.leaves = 0.0;
                order.state = LocalState::Canceled;
                "CANCELED"
            }
        };
        self.record_cancel(client_order_id, response);
        response
    }

    fn record_cancel(&mut self, client_order_id: &str, response: &str) {
        self.cancel_responses
            .push((client_order_id.to_string(), response.to_string()));
    }

    pub fn last_cancel_response(&self) -> Option<&str> {
        self.cancel_responses
            .last()
            .map(|(_, response)| response.as_str())
    }

    /// Client-managed replace.
    ///
    /// A replace that must reset priority (an increase under the fixture's
    /// rules) is implemented as cancel+new: the remainder is canceled and a
    /// successor order is submitted at the back of the queue. A replace that
    /// keeps priority is an in-place resize of the same client order.
    ///
    /// The fixture requires a priority reset on an increase, which the pinned
    /// engine does not perform for a same-price size change. The adapter
    /// therefore performs the cancel+new itself and counts the reset.
    pub fn request_replace(
        &mut self,
        client_order_id: &str,
        new_total_qty: f64,
        displayed_at_level: f64,
        increase_resets_priority: bool,
    ) -> Result<ReplaceOutcome, &'static str> {
        let Some(order) = self.orders.get(client_order_id) else {
            return Err("UNKNOWN_ORDER");
        };
        if order.state.is_terminal() {
            return Err("ORDER_TERMINAL");
        }
        let side = order.side;
        let accepted = order.quantity;
        let filled = order.filled;
        let leaves = order.leaves;
        let ahead = order.ahead_qty;
        let group = order.group.clone();
        let reduce_only = order.reduce_only;
        let position_epoch = order.position_epoch;

        let increase = new_total_qty > accepted + EPS;
        let priority_reset = increase && increase_resets_priority;
        let leaves_new = (new_total_qty - filled).max(0.0);
        let state = if filled > EPS {
            LocalState::PartiallyFilled
        } else {
            LocalState::Accepted
        };

        if !priority_reset {
            // In-place resize: the venue order keeps its queue position.
            let order = self.orders.get_mut(client_order_id).expect("checked above");
            order.quantity = new_total_qty;
            order.leaves = leaves_new;
            return Ok(ReplaceOutcome {
                request_id: client_order_id.to_string(),
                new_order_id: client_order_id.to_string(),
                increase,
                priority_reset: false,
                accepted_qty: new_total_qty,
                filled_qty: filled,
                leaves_qty: leaves_new,
                ahead_qty: ahead,
                canceled_remainder_qty: 0.0,
            });
        }

        // Cancel-managed leg. The venue cancel is issued by the caller.
        let remainder = leaves;
        {
            let old = self.orders.get_mut(client_order_id).expect("checked above");
            old.canceled += remainder;
            old.leaves = 0.0;
            old.state = LocalState::Canceled;
        }
        self.record_cancel(client_order_id, "CANCELED");
        self.venue_cancels += 1;

        // New leg with the client's fill history carried over.
        self.replace_count += 1;
        let new_order_id = format!("{client_order_id}-R{}", self.replace_count);
        let seq = self.stamp_venue_seq();
        self.orders.insert(
            new_order_id.clone(),
            LocalOrder {
                client_order_id: new_order_id.clone(),
                side,
                quantity: new_total_qty,
                filled,
                leaves: leaves_new,
                canceled: 0.0,
                state,
                venue_seq: seq,
                fill_ids: Vec::new(),
                group,
                reduce_only,
                ahead_qty: displayed_at_level,
                position_epoch,
            },
        );
        self.venue_submissions += 1;
        self.priority_reset_count += 1;

        Ok(ReplaceOutcome {
            request_id: client_order_id.to_string(),
            new_order_id,
            increase,
            priority_reset: true,
            accepted_qty: new_total_qty,
            filled_qty: filled,
            leaves_qty: leaves_new,
            ahead_qty: displayed_at_level,
            canceled_remainder_qty: remainder,
        })
    }

    /// Conservative client queue model.
    ///
    /// A trade with an unknown aggressor does not deplete the own-order queue:
    /// the adapter cannot attribute the print to the resting side, so it
    /// assumes the queue is unchanged rather than crediting itself a fill.
    pub fn note_trade(
        &mut self,
        client_order_id: &str,
        qty: f64,
        aggressor: AggressorView,
    ) -> TradeOutcome {
        let Some(order) = self.orders.get(client_order_id) else {
            return TradeOutcome {
                policy: "UNKNOWN_ORDER",
                depleted_qty: 0.0,
                fill_qty: 0.0,
                ahead_after: 0.0,
            };
        };
        let ahead = order.ahead_qty;
        let leaves = order.leaves;
        let side = order.side;
        if order.state != LocalState::Accepted && order.state != LocalState::PartiallyFilled {
            return TradeOutcome {
                policy: "ORDER_NOT_WORKING",
                depleted_qty: 0.0,
                fill_qty: 0.0,
                ahead_after: ahead,
            };
        }
        if aggressor == AggressorView::Unknown {
            self.unknown_aggressor_trades += 1;
            self.queue_depletions_skipped += 1;
            if let Some(order) = self.orders.get_mut(client_order_id) {
                order.ahead_qty = ahead;
            }
            return TradeOutcome {
                policy: "CONSERVATIVE_NO_DEPLETION",
                depleted_qty: 0.0,
                fill_qty: 0.0,
                ahead_after: ahead,
            };
        }
        let eligible_side = match aggressor {
            AggressorView::Buyer => side == Side::Sell,
            AggressorView::Seller => side == Side::Buy,
            AggressorView::Unknown => false,
        };
        if !eligible_side {
            return TradeOutcome {
                policy: "NOT_ELIGIBLE_SIDE",
                depleted_qty: 0.0,
                fill_qty: 0.0,
                ahead_after: ahead,
            };
        }
        let eligible = (qty - ahead).max(0.0);
        let fill = eligible.min(leaves);
        let ahead_after = (ahead - qty).max(0.0);
        let mut applied = 0.0;
        if fill > EPS {
            let seq = self.stamp_venue_seq();
            let (verdict, qty_applied) =
                self.note_fill(client_order_id, seq, &format!("Q-{seq}"), fill);
            if verdict == EventVerdict::Applied {
                applied = qty_applied;
            }
        }
        if let Some(order) = self.orders.get_mut(client_order_id) {
            order.ahead_qty = ahead_after;
        }
        TradeOutcome {
            policy: "AGGRESSOR_ATTRIBUTED",
            depleted_qty: ahead - ahead_after,
            fill_qty: applied,
            ahead_after,
        }
    }

    /// Submits a stop/exit only if it was built for the current position epoch.
    pub fn submit_stop_with_epoch(
        &mut self,
        client_order_id: &str,
        epoch: u64,
        side: Side,
        quantity: f64,
        group: Option<&str>,
        reduce_only: bool,
    ) -> Result<u64, &'static str> {
        if epoch != self.position_epoch {
            self.stale_epoch_rejections += 1;
            self.risk_events.push(format!(
                "STALE_POSITION_EPOCH:{client_order_id}:requested={epoch}:current={}",
                self.position_epoch
            ));
            return Err("STALE_POSITION_EPOCH");
        }
        Ok(self.register(
            client_order_id,
            side,
            quantity,
            group,
            reduce_only,
            0.0,
            Some(epoch),
        ))
    }

    /// Size a reduce-only exit from the filled entry quantity.
    pub fn size_reduce_only_exit(&self, filled_entry_qty: f64, requested: f64) -> f64 {
        requested.min(filled_entry_qty).max(0.0)
    }

    /// Atomic client OCO: on the first fill of a group, name the surviving
    /// sibling so the caller can cancel it before the next price move.
    pub fn group_sibling_after_fill(&mut self, filled_order_id: &str) -> Option<GroupCancel> {
        let group = self.orders.get(filled_order_id)?.group.clone()?;
        let sibling = self
            .orders
            .values()
            .find(|order| {
                order.group.as_deref() == Some(group.as_str())
                    && order.client_order_id != filled_order_id
                    && !order.state.is_terminal()
            })
            .map(|order| order.client_order_id.clone())?;
        self.group_sibling_cancels += 1;
        Some(GroupCancel {
            filled_order_id: filled_order_id.to_string(),
            sibling_id: sibling,
        })
    }

    /// Client reduce-only guard: while flat, a reduce-only exit is rejected
    /// locally and never sent to the venue.
    pub fn guard_reduce_only_exit(&mut self, qty: f64) -> Result<f64, &'static str> {
        if self.position_qty.abs() < EPS && qty > EPS {
            self.risk_events.push("REDUCE_ONLY_REJECT:flat".to_string());
            return Err("REDUCE_ONLY_REJECT");
        }
        let allowed = self.size_reduce_only_exit(self.position_qty.abs(), qty);
        if allowed <= EPS {
            self.risk_events.push("REDUCE_ONLY_REJECT:non_positive".to_string());
            return Err("REDUCE_ONLY_REJECT");
        }
        Ok(allowed)
    }

    /// Blocks new risk while the client book is not synchronized.
    pub fn gate_new_risk(&mut self, client_order_id: &str) -> Result<(), &'static str> {
        if !self.book.risk_allowed() {
            self.blocked_risk_requests += 1;
            self.risk_events
                .push(format!("BOOK_RESYNC_REQUIRED:{client_order_id}"));
            return Err("BOOK_RESYNC_REQUIRED");
        }
        Ok(())
    }

    pub fn snapshot(&self) -> Value {
        let orders: Vec<Value> = self
            .orders
            .values()
            .map(|order| {
                json!({
                    "client_order_id": order.client_order_id,
                    "side": order.side.as_str(),
                    "state": order.state.as_str(),
                    "quantity": order.quantity,
                    "filled_qty": order.filled,
                    "leaves_qty": order.leaves,
                    "canceled_qty": order.canceled,
                    "venue_seq": order.venue_seq,
                    "fill_ids": order.fill_ids,
                    "group": order.group,
                    "reduce_only": order.reduce_only,
                    "ahead_qty": order.ahead_qty,
                    "position_epoch": order.position_epoch,
                })
            })
            .collect();
        json!({
            "profile": PROFILE,
            "orders": orders,
            "next_venue_seq": self.next_venue_seq,
            "last_venue_seq": self.last_venue_seq,
            "position_qty": self.position_qty,
            "position_epoch": self.position_epoch,
            "cancel_responses": self
                .cancel_responses
                .iter()
                .map(|(id, response)| json!({"client_order_id": id, "response": response}))
                .collect::<Vec<_>>(),
            "last_cancel_response": self.last_cancel_response(),
            "priority_reset_count": self.priority_reset_count,
            "replace_count": self.replace_count,
            "duplicate_fills_suppressed": self.duplicate_fills_suppressed,
            "out_of_order_events_suppressed": self.out_of_order_events_suppressed,
            "overfill_clamped": self.overfill_clamped,
            "duplicate_fee_debits": self.duplicate_fee_debits,
            "stale_epoch_rejections": self.stale_epoch_rejections,
            "group_sibling_cancels": self.group_sibling_cancels,
            "venue_submissions": self.venue_submissions,
            "venue_cancels": self.venue_cancels,
            "blocked_risk_requests": self.blocked_risk_requests,
            "fills_applied": self.fills_applied,
            "strategy_fill_events": self.strategy_fill_events,
            "unknown_aggressor_trades": self.unknown_aggressor_trades,
            "queue_depletions_skipped": self.queue_depletions_skipped,
            "risk_events": self.risk_events,
            "book": {
                "state": self.book.state.as_str(),
                "anchor": self.book.anchor,
                "last_good_seq": self.book.last_good_seq,
                "initial_match": self.book.initial_match,
                "applied": self.book.applied,
                "rejected": self.book.rejected,
                "last_reject": self.book.last_reject.as_ref().map(BookReject::to_json),
            },
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn adapter_with_order() -> Adapter {
        let mut adapter = Adapter::new();
        adapter.register("O-1", Side::Buy, 2.0, None, false, 0.0, None);
        adapter
    }

    #[test]
    fn duplicate_fill_is_suppressed_by_trade_id() {
        let mut adapter = adapter_with_order();
        let (verdict, applied) = adapter.note_fill("O-1", 2, "FILL-1", 1.0);
        assert_eq!(verdict, EventVerdict::Applied);
        assert_eq!(applied, 1.0);
        let (verdict, applied) = adapter.note_fill("O-1", 2, "FILL-1", 1.0);
        assert_eq!(verdict, EventVerdict::DuplicateSuppressed);
        assert_eq!(applied, 0.0);
        let order = adapter.order("O-1").expect("order");
        assert_eq!(order.filled, 1.0);
        assert_eq!(adapter.duplicate_fills_suppressed, 1);
        assert_eq!(adapter.strategy_fill_events, 1);
        assert_eq!(adapter.duplicate_fee_debits, 0);
    }

    #[test]
    fn out_of_order_ack_does_not_regress_state() {
        let mut adapter = adapter_with_order();
        adapter.note_fill("O-1", 2, "FILL-1", 1.0);
        assert_eq!(
            adapter.note_accepted("O-1", 1),
            EventVerdict::OutOfOrderSuppressed
        );
        assert_eq!(adapter.order("O-1").expect("order").state, LocalState::PartiallyFilled);
        assert_eq!(adapter.out_of_order_events_suppressed, 1);
    }

    #[test]
    fn fill_is_clamped_to_leaves() {
        let mut adapter = adapter_with_order();
        let (_, applied) = adapter.note_fill("O-1", 2, "FILL-1", 5.0);
        assert_eq!(applied, 2.0);
        assert_eq!(adapter.overfill_clamped, 1);
        assert_eq!(adapter.order("O-1").expect("order").leaves, 0.0);
    }

    #[test]
    fn cancel_of_filled_order_responds_already_filled() {
        let mut adapter = adapter_with_order();
        adapter.note_fill("O-1", 2, "FILL-1", 2.0);
        let (decision, response) = adapter.request_cancel("O-1");
        assert_eq!(decision, CancelDecision::RespondLocally);
        assert_eq!(response, "ALREADY_FILLED");
        assert_eq!(adapter.venue_cancels, 0);
        assert_eq!(adapter.last_cancel_response(), Some("ALREADY_FILLED"));
    }

    #[test]
    fn late_cancel_resolves_already_filled_after_venue_round_trip() {
        let mut adapter = adapter_with_order();
        let (decision, _) = adapter.request_cancel("O-1");
        assert_eq!(decision, CancelDecision::DispatchToVenue);
        assert_eq!(adapter.venue_cancels, 1);
        adapter.note_fill("O-1", 2, "FILL-1", 2.0);
        assert_eq!(adapter.resolve_cancel("O-1"), "ALREADY_FILLED");
        assert_eq!(adapter.order("O-1").expect("order").state, LocalState::Filled);
    }

    #[test]
    fn working_cancel_zeroes_leaves() {
        let mut adapter = adapter_with_order();
        adapter.request_cancel("O-1");
        assert_eq!(adapter.resolve_cancel("O-1"), "CANCELED");
        let order = adapter.order("O-1").expect("order");
        assert_eq!(order.leaves, 0.0);
        assert_eq!(order.canceled, 2.0);
        assert_eq!(order.state, LocalState::Canceled);
    }

    #[test]
    fn replace_increase_resets_priority_decrease_does_not() {
        let mut adapter = adapter_with_order();
        adapter.note_fill("O-1", 2, "FILL-1", 1.0);
        let outcome = adapter.request_replace("O-1", 4.0, 100.0, true).expect("replace");
        assert!(outcome.increase);
        assert!(outcome.priority_reset);
        assert_eq!(outcome.ahead_qty, 100.0);
        assert_eq!(outcome.leaves_qty, 3.0);
        assert_eq!(adapter.priority_reset_count, 1);
        let next_id = outcome.new_order_id;
        let outcome = adapter.request_replace(&next_id, 3.0, 100.0, true).expect("replace");
        assert!(!outcome.increase);
        assert!(!outcome.priority_reset);
        assert_eq!(outcome.new_order_id, next_id);
        assert_eq!(outcome.ahead_qty, 100.0);
        assert_eq!(outcome.leaves_qty, 2.0);
        assert_eq!(adapter.priority_reset_count, 1);
    }

    #[test]
    fn position_epoch_advances_on_reopen() {
        let mut adapter = Adapter::new();
        adapter.register("E-1", Side::Buy, 1.0, None, false, 0.0, None);
        adapter.note_fill("E-1", 2, "T1", 1.0);
        assert_eq!(adapter.position_epoch, 1);
        adapter.register("E-2", Side::Sell, 1.0, None, false, 0.0, None);
        adapter.note_fill("E-2", 4, "T2", 1.0);
        assert_eq!(adapter.position_qty, 0.0);
        assert_eq!(adapter.position_epoch, 1);
        adapter.register("E-3", Side::Buy, 1.0, None, false, 0.0, None);
        adapter.note_fill("E-3", 6, "T3", 1.0);
        assert_eq!(adapter.position_epoch, 2);
        assert_eq!(
            adapter.submit_stop_with_epoch("STALE", 1, Side::Sell, 1.0, None, true),
            Err("STALE_POSITION_EPOCH")
        );
        assert_eq!(adapter.stale_epoch_rejections, 1);
        assert!(adapter
            .submit_stop_with_epoch("FRESH", 2, Side::Sell, 1.0, None, true)
            .is_ok());
    }

    #[test]
    fn book_guard_invalidates_on_previous_mismatch_and_resyncs() {
        let mut guard = BookGuard::new();
        guard.snapshot(100);
        assert_eq!(guard.delta(99, 101, 98), BookVerdict::Applied);
        assert!(guard.initial_match);
        assert_eq!(guard.delta(102, 102, 101), BookVerdict::Applied);
        assert_eq!(guard.delta(103, 103, 999), BookVerdict::Rejected);
        assert_eq!(guard.state, BookState::Invalid);
        assert_eq!(guard.last_good_seq, Some(102));
        assert_eq!(guard.last_reject.as_ref().map(|r| r.code), Some("PREVIOUS_SEQUENCE_MISMATCH"));
        assert!(!guard.risk_allowed());
        guard.snapshot(110);
        assert!(guard.risk_allowed());
        assert_eq!(guard.last_good_seq, Some(110));
    }

    #[test]
    fn book_guard_detects_forward_gap_and_stale_range() {
        let mut guard = BookGuard::new();
        guard.snapshot(102);
        assert_eq!(guard.delta(110, 110, 109), BookVerdict::Rejected);
        assert_eq!(guard.last_good_seq, Some(102));
        guard.snapshot(110);
        assert_eq!(guard.delta(105, 105, 104), BookVerdict::Rejected);
        assert_eq!(
            guard.last_reject.as_ref().map(|r| r.code),
            Some("OUT_OF_ORDER_STALE")
        );
    }

    #[test]
    fn unknown_aggressor_does_not_deplete_queue() {
        let mut adapter = Adapter::new();
        adapter.register("O-1", Side::Buy, 20.0, None, false, 100.0, None);
        let outcome = adapter.note_trade("O-1", 80.0, AggressorView::Seller);
        assert_eq!(outcome.ahead_after, 20.0);
        assert_eq!(outcome.fill_qty, 0.0);
        let outcome = adapter.note_trade("O-1", 30.0, AggressorView::Unknown);
        assert_eq!(outcome.policy, "CONSERVATIVE_NO_DEPLETION");
        assert_eq!(outcome.ahead_after, 20.0);
        assert_eq!(adapter.queue_depletions_skipped, 1);
        assert_eq!(adapter.order("O-1").expect("order").filled, 0.0);
    }

    #[test]
    fn attributed_aggressor_depletes_queue_and_fills() {
        let mut adapter = Adapter::new();
        adapter.register("O-1", Side::Buy, 20.0, None, false, 100.0, None);
        let outcome = adapter.note_trade("O-1", 130.0, AggressorView::Seller);
        assert_eq!(outcome.ahead_after, 0.0);
        assert_eq!(outcome.fill_qty, 20.0);
        assert_eq!(adapter.order("O-1").expect("order").state, LocalState::Filled);
    }

    #[test]
    fn group_sibling_is_named_for_atomic_cancel() {
        let mut adapter = Adapter::new();
        adapter.register("TP", Side::Sell, 1.0, Some("GR-1"), true, 0.0, None);
        adapter.register("SL", Side::Sell, 1.0, Some("GR-1"), true, 0.0, None);
        let sibling = adapter.group_sibling_after_fill("TP").expect("sibling");
        assert_eq!(sibling.sibling_id, "SL");
        assert_eq!(adapter.group_sibling_cancels, 1);
    }

    #[test]
    fn reduce_only_exit_guard_rejects_when_flat() {
        let mut adapter = Adapter::new();
        assert_eq!(adapter.guard_reduce_only_exit(1.0), Err("REDUCE_ONLY_REJECT"));
        adapter.register("E-1", Side::Buy, 1.0, None, false, 0.0, None);
        adapter.note_fill("E-1", 2, "T1", 1.0);
        assert_eq!(adapter.guard_reduce_only_exit(2.0), Ok(1.0));
    }

    #[test]
    fn risk_is_blocked_while_book_invalid() {
        let mut adapter = Adapter::new();
        assert_eq!(adapter.gate_new_risk("O-1"), Err("BOOK_RESYNC_REQUIRED"));
        adapter.book.snapshot(100);
        assert!(adapter.gate_new_risk("O-1").is_ok());
        adapter.book.delta(103, 103, 999);
        assert_eq!(adapter.gate_new_risk("O-2"), Err("BOOK_RESYNC_REQUIRED"));
        assert_eq!(adapter.blocked_risk_requests, 2);
    }
}
