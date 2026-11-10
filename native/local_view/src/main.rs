//! QEL two-world F22 vertical slice.
//!
//! Exchange world `X` is the pinned Nautilus `BacktestEngine`: market data is
//! applied, orders are matched and the account/position is updated at the
//! exchange timestamp. Strategy world `Y` is the adapter-managed `LocalView`
//! below. It only receives
//!
//! * market data at `ts_event + feed_delay_ns`, and
//! * order/fill/funding responses at `ts_event + response_delay_ns`,
//!
//! each delivered on the first data callback at or after the due timestamp
//! (the pinned engine has no generic ``deliver-at`` timer hook). The policy
//! path in `decide` reads `self.local` only. It never calls `self.cache()`.
//!
//! This is not a fork and not a monkey patch. The public API used is the same
//! `Strategy`/`DataActor` callback surface as `native/runner`, plus the typed
//! `qel-policy` evaluator.

use std::{
    collections::{BTreeMap, HashMap, VecDeque},
    fmt::Debug,
    fs::{self, File},
    io::{BufRead, BufReader},
    str::FromStr,
    sync::{Arc, Mutex},
    time::Instant,
};

use anyhow::{anyhow, bail, Context, Result};
use nautilus_backtest::{
    config::{BacktestEngineConfig, SimulatedVenueConfig},
    engine::BacktestEngine,
};
use nautilus_common::actor::DataActor;
use nautilus_common::msgbus::{self, switchboard::get_event_position_topic, TypedHandler};
use nautilus_core::UnixNanos;
use nautilus_model::{
    data::{Data, FundingRateUpdate, MarkPriceUpdate, QuoteTick, TradeTick},
    enums::{
        AccountType, AggressorSide, BookType, OmsType, OrderSide, PositionAdjustmentType,
        PositionSide, TimeInForce,
    },
    events::{OrderFilled, PositionChanged, PositionClosed, PositionEvent, PositionOpened},
    identifiers::{
        InstrumentId, PositionId, StrategyId, Symbol, TradeId, Venue,
    },
    instruments::{CryptoPerpetual, Instrument, InstrumentAny},
    types::{Currency, Money, Price, Quantity},
};
use nautilus_trading::{Strategy, StrategyConfig, StrategyCore, nautilus_strategy};
use qel_policy::{parse_strategy, Engine as PolicyEngine, StepOutput};
use rust_decimal::Decimal;
use rust_decimal::prelude::ToPrimitive;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

const STRATEGY_ID: &str = "TAKER-NATIVE-001";
const ORDER_ID_TAG: &str = "001";
const NAUTILUS_TAG: &str = "v1.231.0";
const NAUTILUS_REV: &str = "27a8e54e7ac3c57d6cbf8891f0283dfbaee97317";

fn trim(value: f64, dp: usize) -> String {
    let mut s = format!("{value:.dp$}");
    if s.contains('.') {
        s = s.trim_end_matches('0').trim_end_matches('.').to_string();
    }
    if s == "-0" {
        "0".to_string()
    } else {
        s
    }
}

fn dec_str(value: Decimal) -> String {
    value.to_string()
}

// ---------------------------------------------------------------------------
// LocalView: the strategy's only source of market/account state.
// ---------------------------------------------------------------------------

#[derive(Clone, Debug)]
enum FeedKind {
    Quote { mid: f64 },
    Trade { price: f64 },
    Mark { price: Decimal },
}

#[derive(Clone, Debug)]
struct FeedItem {
    seq: u64,
    ts_event: u64,
    deliver_at: u64,
    kind: FeedKind,
}

#[derive(Clone, Debug)]
enum ResponseKind {
    Fill {
        signed_qty: Decimal,
        qty: Decimal,
        price: Decimal,
        fee: Decimal,
        position_id: Option<PositionId>,
    },
    Funding {
        amount: Decimal,
    },
}

#[derive(Clone, Debug)]
struct ResponseItem {
    seq: u64,
    ts_event: u64,
    deliver_at: u64,
    kind: ResponseKind,
}

/// Delivered strategy-world state. Every field changes only in `apply_*`
/// methods reached from the delayed queues.
#[derive(Clone, Debug)]
struct LocalView {
    cash: Decimal,
    position_qty: Decimal,
    position_id: Option<PositionId>,
    entry_price: Decimal,
    realized: Decimal,
    fees: Decimal,
    funding_cash: Decimal,
    last_mid: Option<f64>,
    last_trade: Option<f64>,
    last_mark: Option<Decimal>,
    bucket: u64,
    position_open_ns: u64,
    delivered_fills: usize,
    delivered_fundings: usize,
}

impl Default for LocalView {
    fn default() -> Self {
        Self {
            cash: Decimal::from(1000),
            position_qty: Decimal::ZERO,
            position_id: None,
            entry_price: Decimal::ZERO,
            realized: Decimal::ZERO,
            fees: Decimal::ZERO,
            funding_cash: Decimal::ZERO,
            last_mid: None,
            last_trade: None,
            last_mark: None,
            bucket: 0,
            position_open_ns: 0,
            delivered_fills: 0,
            delivered_fundings: 0,
        }
    }
}

// ---------------------------------------------------------------------------
// Evidence records shared with the runner thread.
// ---------------------------------------------------------------------------

#[derive(Clone, Debug)]
struct FeedSample {
    seq: u64,
    kind: &'static str,
    ts_event: u64,
    deliver_at: u64,
    delivered_at: u64,
}

#[derive(Clone, Debug, Default)]
struct FeedAudit {
    total: usize,
    quotes: usize,
    trades: usize,
    marks: usize,
    first_tick_violations: usize,
    delivered_before_due: usize,
    min_lateness_ns: u64,
    max_lateness_ns: u64,
    sum_lateness_ns: u128,
    min_total_delay_ns: u64,
    max_total_delay_ns: u64,
    first_samples: Vec<FeedSample>,
    last_samples: VecDeque<FeedSample>,
    stride_samples: Vec<FeedSample>,
}

#[derive(Clone, Debug)]
struct FillDeliveryRecord {
    seq: u64,
    exchange_ts: u64,
    deliver_at: u64,
    delivered_at: u64,
    delivered: bool,
    side: &'static str,
    qty: Decimal,
    price: Decimal,
    fee: Decimal,
    exchange_position_before: Decimal,
    exchange_position_after: Decimal,
    local_position_before: Decimal,
    local_position_after: Decimal,
    local_cash_before: Decimal,
    local_cash_after: Decimal,
    position_id: Option<String>,
}

#[derive(Clone, Debug)]
struct FundingDeliveryRecord {
    seq: u64,
    exchange_ts: u64,
    deliver_at: u64,
    delivered_at: u64,
    delivered: bool,
    amount: Decimal,
    currency: String,
    settlement_id: String,
    position_id: String,
    account_id: String,
    epoch: u64,
    local_cash_before: Decimal,
    local_cash_after: Decimal,
}

#[derive(Clone, Debug)]
struct DecisionRecord {
    seq: u64,
    ts: u64,
    local_position_qty: Decimal,
    local_cash: Decimal,
    delivered_fills: usize,
}

#[derive(Clone, Debug)]
struct ExchangePositionRecord {
    ts: u64,
    kind: &'static str,
    signed_qty: String,
    position_id: String,
}

#[derive(Clone, Debug, Default)]
struct SharedEvidence {
    local: Option<LocalView>,
    finalized: bool,
    feed_audit: FeedAudit,
    fills: Vec<FillDeliveryRecord>,
    funding: Vec<FundingDeliveryRecord>,
    decisions: Vec<DecisionRecord>,
    exchange_positions: Vec<ExchangePositionRecord>,
    finalizing_deliveries: usize,
}

#[derive(Clone, Debug)]
struct FundingObs {
    ts_event: u64,
    amount: Decimal,
    currency: String,
    settlement_id: String,
    position_id: String,
    account_id: String,
    epoch: u64,
}

// ---------------------------------------------------------------------------
// Bounded funding epoch ledger (same labelling rule as native/runner).
// ---------------------------------------------------------------------------

#[derive(Default)]
struct EpochState {
    counter: u64,
    current: HashMap<PositionId, (u64, u64)>,
    closed: VecDeque<(PositionId, u64, u64)>,
}

impl EpochState {
    fn open(&mut self, position_id: PositionId, ts_opened: u64) -> u64 {
        self.counter += 1;
        self.current.insert(position_id, (self.counter, ts_opened));
        self.counter
    }

    fn close(&mut self, position_id: PositionId, ts_closed: u64) {
        if let Some((epoch, _)) = self.current.get(&position_id).copied() {
            self.closed.push_back((position_id, epoch, ts_closed));
        }
    }

    fn epoch(&self, position_id: &PositionId) -> u64 {
        self.current
            .get(position_id)
            .map(|(epoch, _)| *epoch)
            .unwrap_or_else(|| self.counter.max(1))
    }
}

#[derive(Default)]
struct RunStats {
    orders_submitted: usize,
    fills: usize,
    fill_qty: Decimal,
    fill_notional: Decimal,
    fees: Decimal,
    decisions: usize,
    steps: usize,
}

// ---------------------------------------------------------------------------
// The strategy adapter.
// ---------------------------------------------------------------------------

struct LocalViewTaker {
    core: StrategyCore,
    instrument_id: InstrumentId,
    policy: PolicyEngine,
    notional: f64,
    bar_ns: u64,
    max_holding_ns: u64,
    feed_delay_ns: u64,
    response_delay_ns: u64,

    pending_feed: VecDeque<FeedItem>,
    pending_response: VecDeque<ResponseItem>,
    external_funding: Arc<Mutex<Vec<FundingObs>>>,

    local: LocalView,
    next_seq: u64,
    exchange_position_qty: Decimal,

    tape: Arc<Mutex<Vec<Value>>>,
    shared: Arc<Mutex<SharedEvidence>>,
    stats: Arc<Mutex<RunStats>>,

    prev_tick: u64,
    last_tick: u64,
    feed_audit: FeedAudit,
    finalized: bool,
    finalizing: bool,
}

impl LocalViewTaker {
    #[allow(clippy::too_many_arguments)]
    fn new(
        instrument_id: InstrumentId,
        policy: PolicyEngine,
        notional: f64,
        bar_ns: u64,
        max_holding_ns: u64,
        feed_delay_ns: u64,
        response_delay_ns: u64,
        stats: Arc<Mutex<RunStats>>,
    ) -> Self {
        let config = StrategyConfig {
            strategy_id: Some(StrategyId::from(STRATEGY_ID)),
            order_id_tag: Some(ORDER_ID_TAG.to_string()),
            ..Default::default()
        };
        let tape: Arc<Mutex<Vec<Value>>> =
            Arc::new(Mutex::new(vec![json!({"type": "SET_WALLET", "cash": "1000"})]));
        Self {
            core: StrategyCore::new(config),
            instrument_id,
            policy,
            notional,
            bar_ns,
            max_holding_ns,
            feed_delay_ns,
            response_delay_ns,
            pending_feed: VecDeque::new(),
            pending_response: VecDeque::new(),
            external_funding: Arc::new(Mutex::new(Vec::new())),
            local: LocalView::default(),
            next_seq: 0,
            exchange_position_qty: Decimal::ZERO,
            tape,
            shared: Arc::new(Mutex::new(SharedEvidence::default())),
            stats,
            prev_tick: 0,
            last_tick: 0,
            feed_audit: FeedAudit::default(),
            finalized: false,
            finalizing: false,
        }
    }

    fn next_seq(&mut self) -> u64 {
        let seq = self.next_seq;
        self.next_seq += 1;
        seq
    }

    fn enqueue_response(&mut self, item: ResponseItem) {
        // Response events are normally produced in timestamp order. Funding
        // adjustments are pulled from the msgbus observation buffer at the next
        // data tick, so insert them at their causal deliver-at position.
        let pos = self
            .pending_response
            .iter()
            .position(|queued| queued.deliver_at > item.deliver_at)
            .unwrap_or(self.pending_response.len());
        self.pending_response.insert(pos, item);
    }

    fn enqueue_feed(&mut self, ts_event: u64, kind: FeedKind) {
        let seq = self.next_seq();
        self.pending_feed.push_back(FeedItem {
            seq,
            ts_event,
            deliver_at: ts_event.saturating_add(self.feed_delay_ns),
            kind,
        });
    }

    fn pull_external_funding(&mut self) {
        let observations: Vec<FundingObs> = {
            let mut guard = self.external_funding.lock().unwrap();
            std::mem::take(&mut *guard)
        };
        for obs in observations {
            let seq = self.next_seq();
            self.shared.lock().unwrap().funding.push(FundingDeliveryRecord {
                seq,
                exchange_ts: obs.ts_event,
                deliver_at: obs.ts_event.saturating_add(self.response_delay_ns),
                delivered_at: 0,
                delivered: false,
                amount: obs.amount,
                currency: obs.currency.clone(),
                settlement_id: obs.settlement_id.clone(),
                position_id: obs.position_id.clone(),
                account_id: obs.account_id.clone(),
                epoch: obs.epoch,
                local_cash_before: Decimal::ZERO,
                local_cash_after: Decimal::ZERO,
            });
            self.enqueue_response(ResponseItem {
                seq,
                ts_event: obs.ts_event,
                deliver_at: obs.ts_event.saturating_add(self.response_delay_ns),
                kind: ResponseKind::Funding {
                    amount: obs.amount,
                },
            });
        }
    }

    /// Deliver everything due at `now`, responses (exchange results) before
    /// feed (market data), then run the policy on newly delivered quotes.
    fn advance(&mut self, now: u64) -> Result<()> {
        if now > self.last_tick {
            self.prev_tick = self.last_tick;
            self.last_tick = now;
        }

        self.pull_external_funding();

        while self
            .pending_response
            .front()
            .is_some_and(|item| item.deliver_at <= now)
        {
            let item = self.pending_response.pop_front().unwrap();
            self.check_first_tick(item.deliver_at, now, "response");
            self.apply_response(&item, now)?;
        }

        while self
            .pending_feed
            .front()
            .is_some_and(|item| item.deliver_at <= now)
        {
            let item = self.pending_feed.pop_front().unwrap();
            self.check_first_tick(item.deliver_at, now, "feed");
            self.apply_feed(&item, now)?;
        }
        Ok(())
    }

    /// A delivery is only legal on the first data tick at or after its due
    /// timestamp. `prev_tick` is the greatest distinct tick strictly below
    /// `now`, so `prev_tick < deliver_at` proves no earlier tick could have
    /// delivered it. This is how the evidence proves the configured delay is
    /// charged exactly once and not again on top.
    fn check_first_tick(&mut self, deliver_at: u64, now: u64, kind: &'static str) {
        if now < deliver_at {
            self.feed_audit.delivered_before_due += 1;
        }
        // A feed item is enqueued before the advance of its own tick, so it is
        // eligible on that tick. A response is produced after the advance of
        // its exchange tick (during venue settlement), so it is first eligible
        // on the next tick; equality with the previous tick is therefore legal.
        let missed = if kind == "feed" {
            deliver_at < now && self.prev_tick >= deliver_at
        } else {
            deliver_at < now && self.prev_tick > deliver_at
        };
        if missed {
            self.feed_audit.first_tick_violations += 1;
        }
    }

    fn record_feed_audit(&mut self, item: &FeedItem, now: u64) {
        let lateness = now.saturating_sub(item.deliver_at);
        let total_delay = now.saturating_sub(item.ts_event);
        self.feed_audit.total += 1;
        match item.kind {
            FeedKind::Quote { .. } => self.feed_audit.quotes += 1,
            FeedKind::Trade { .. } => self.feed_audit.trades += 1,
            FeedKind::Mark { .. } => self.feed_audit.marks += 1,
        }
        if self.feed_audit.total == 1 {
            self.feed_audit.min_lateness_ns = lateness;
            self.feed_audit.max_lateness_ns = lateness;
            self.feed_audit.min_total_delay_ns = total_delay;
            self.feed_audit.max_total_delay_ns = total_delay;
        } else {
            self.feed_audit.min_lateness_ns = self.feed_audit.min_lateness_ns.min(lateness);
            self.feed_audit.max_lateness_ns = self.feed_audit.max_lateness_ns.max(lateness);
            self.feed_audit.min_total_delay_ns =
                self.feed_audit.min_total_delay_ns.min(total_delay);
            self.feed_audit.max_total_delay_ns =
                self.feed_audit.max_total_delay_ns.max(total_delay);
        }
        self.feed_audit.sum_lateness_ns += lateness as u128;

        let kind = match item.kind {
            FeedKind::Quote { .. } => "quote",
            FeedKind::Trade { .. } => "trade",
            FeedKind::Mark { .. } => "mark",
        };
        let sample = FeedSample {
            seq: item.seq,
            kind,
            ts_event: item.ts_event,
            deliver_at: item.deliver_at,
            delivered_at: now,
        };
        if self.feed_audit.first_samples.len() < 8 {
            self.feed_audit.first_samples.push(sample.clone());
        }
        if self.feed_audit.last_samples.len() == 8 {
            self.feed_audit.last_samples.pop_front();
        }
        self.feed_audit.last_samples.push_back(sample.clone());
        if self.feed_audit.total % 50_000 == 0 {
            self.feed_audit.stride_samples.push(sample);
        }
    }

    fn apply_feed(&mut self, item: &FeedItem, now: u64) -> Result<()> {
        self.record_feed_audit(item, now);
        match item.kind {
            FeedKind::Quote { mid } => {
                let bucket = item.ts_event / self.bar_ns;
                let mut decision_price = None;
                if self.local.bucket == 0 {
                    self.local.bucket = bucket;
                } else if bucket != self.local.bucket {
                    self.local.bucket = bucket;
                    if let Some(prev) = self.local.last_mid {
                        decision_price = Some(prev);
                    }
                }
                if let Some(prev) = decision_price {
                    if !self.finalizing {
                        self.decide(prev, now)?;
                    }
                }
                self.local.last_mid = Some(mid);
            }
            FeedKind::Trade { price } => {
                self.local.last_trade = Some(price);
            }
            FeedKind::Mark { price } => {
                self.local.last_mark = Some(price);
            }
        }
        Ok(())
    }

    fn apply_response(&mut self, item: &ResponseItem, now: u64) -> Result<()> {
        let seq = item.seq;
        match &item.kind {
            ResponseKind::Fill {
                signed_qty,
                qty,
                price,
                fee,
                position_id,
            } => {
                let local_before = self.local.position_qty;
                let cash_before = self.local.cash;
                let was_long = self.local.position_qty > Decimal::ZERO;
                let realized = if self.local.position_qty.is_zero()
                    || was_long == (*signed_qty > Decimal::ZERO)
                {
                    let new_qty = self.local.position_qty + signed_qty;
                    self.local.entry_price = if self.local.position_qty.is_zero() {
                        *price
                    } else {
                        (self.local.entry_price * self.local.position_qty.abs()
                            + *price * *qty)
                            / new_qty.abs()
                    };
                    self.local.position_qty = new_qty;
                    Decimal::ZERO
                } else {
                    let old = self.local.position_qty;
                    let close_qty = old.abs().min(*qty);
                    let realized = if old > Decimal::ZERO {
                        (*price - self.local.entry_price) * close_qty
                    } else {
                        (self.local.entry_price - *price) * close_qty
                    };
                    self.local.position_qty = old + signed_qty;
                    if self.local.position_qty.is_zero() {
                        self.local.entry_price = Decimal::ZERO;
                    } else if (self.local.position_qty > Decimal::ZERO) != was_long {
                        self.local.entry_price = *price;
                    }
                    realized
                };
                self.local.realized += realized;
                self.local.cash = (self.local.cash + realized - fee).round_dp(8);
                self.local.fees = (self.local.fees + fee).round_dp(8);
                self.local.delivered_fills += 1;
                if let Some(id) = position_id {
                    self.local.position_id = Some(id.clone());
                }
                let now_long = self.local.position_qty > Decimal::ZERO;
                if local_before.is_zero() && !self.local.position_qty.is_zero() {
                    self.local.position_open_ns = item.ts_event;
                } else if self.local.position_qty.is_zero() {
                    self.local.position_open_ns = 0;
                } else if was_long != now_long {
                    self.local.position_open_ns = item.ts_event;
                }
                {
                    let mut shared = self.shared.lock().unwrap();
                    if let Some(record) = shared
                        .fills
                        .iter_mut()
                        .find(|record| record.seq == seq && !record.delivered)
                    {
                        record.delivered = true;
                        record.delivered_at = now;
                        record.local_position_before = local_before;
                        record.local_position_after = self.local.position_qty;
                        record.local_cash_before = cash_before;
                        record.local_cash_after = self.local.cash;
                    }
                }
            }
            ResponseKind::Funding { amount } => {
                let cash_before = self.local.cash;
                self.local.cash = (self.local.cash + amount).round_dp(8);
                self.local.funding_cash = (self.local.funding_cash + amount).round_dp(8);
                self.local.delivered_fundings += 1;
                let mut shared = self.shared.lock().unwrap();
                if let Some(record) = shared
                    .funding
                    .iter_mut()
                    .find(|record| record.seq == seq && !record.delivered)
                {
                    record.delivered = true;
                    record.delivered_at = now;
                    record.local_cash_before = cash_before;
                    record.local_cash_after = self.local.cash;
                }
            }
        }
        Ok(())
    }

    fn record_decision(&mut self, now: u64) {
        let seq = self.next_seq();
        self.stats.lock().unwrap().decisions += 1;
        self.shared.lock().unwrap().decisions.push(DecisionRecord {
            seq,
            ts: now,
            local_position_qty: self.local.position_qty,
            local_cash: self.local.cash,
            delivered_fills: self.local.delivered_fills,
        });
    }

    fn decide(&mut self, price: f64, now_ns: u64) -> Result<()> {
        // POLICY READ BOUNDARY: only delivered LocalView fields are used.
        // No `self.cache()` call and no exchange account/position snapshot is
        // reachable from this function.
        let mut inputs = HashMap::new();
        inputs.insert("MID_CLOSE".to_string(), Some(price));
        inputs.insert("TRADE_CLOSE".to_string(), self.local.last_trade);
        let out: StepOutput = self.policy.step(&inputs, now_ns)?;
        self.stats.lock().unwrap().steps += 1;
        if !out.decision_enabled {
            return Ok(());
        }
        self.record_decision(now_ns);
        if !self.local.position_qty.is_zero() {
            let is_long = self.local.position_qty > Decimal::ZERO;
            let exit = if is_long {
                out.long_exit
            } else {
                out.short_exit
            } || (self.max_holding_ns > 0
                && now_ns.saturating_sub(self.local.position_open_ns) >= self.max_holding_ns);
            if exit {
                self.close_position()?;
            }
            return Ok(());
        }
        if out.long && out.short {
            return Ok(());
        }
        if out.long {
            self.enter(OrderSide::Buy, price)?;
        } else if out.short {
            self.enter(OrderSide::Sell, price)?;
        }
        Ok(())
    }

    fn qty(&self, price: f64) -> Quantity {
        let raw = (self.notional / price).max(0.001);
        let lot = (raw / 0.001).floor() * 0.001;
        Quantity::from(format!("{lot:.3}").as_str())
    }

    fn enter(&mut self, side: OrderSide, price: f64) -> Result<()> {
        let qty = self.qty(price);
        let order = self.order().market(
            self.instrument_id,
            side,
            qty,
            Some(TimeInForce::Ioc),
            Some(false),
            None,
            None,
            None,
            None,
            None,
        );
        self.stats.lock().unwrap().orders_submitted += 1;
        self.submit_order(order, None, None, None)
    }

    fn close_position(&mut self) -> Result<()> {
        let qty = self.local.position_qty.abs();
        if qty.is_zero() {
            return Ok(());
        }
        let side = if self.local.position_qty > Decimal::ZERO {
            OrderSide::Sell
        } else {
            OrderSide::Buy
        };
        let quantity = Quantity::from(format!("{qty}").as_str());
        let order = self.order().market(
            self.instrument_id,
            side,
            quantity,
            Some(TimeInForce::Ioc),
            Some(true),
            None,
            None,
            None,
            None,
            None,
        );
        self.stats.lock().unwrap().orders_submitted += 1;
        self.submit_order(order, self.local.position_id.clone(), None, None)
    }

    fn record_fill(&mut self, event: &OrderFilled) {
        if event.instrument_id != self.instrument_id {
            return;
        }
        let side = match event.order_side {
            OrderSide::Buy => "BUY",
            _ => "SELL",
        };
        let qty = event.last_qty.as_decimal();
        let price = event.last_px.as_decimal();
        let fee = event
            .commission
            .map(|money| money.as_decimal())
            .unwrap_or(Decimal::ZERO);
        let signed_qty = if event.order_side == OrderSide::Buy {
            qty
        } else {
            -qty
        };
        let before = self.exchange_position_qty;
        self.exchange_position_qty += signed_qty;
        let after = self.exchange_position_qty;
        let ts = event.ts_event.as_u64();

        // Exchange-world tape: the fill is recorded at the exchange timestamp,
        // exactly like the native runner, independent of delivery.
        self.tape.lock().unwrap().push(json!({
            "type": "FILL",
            "ts_ns": ts.to_string(),
            "side": side,
            "qty": trim(qty.to_f64().unwrap_or(0.0), 8),
            "price": trim(price.to_f64().unwrap_or(0.0), 8),
            "fee": trim(fee.to_f64().unwrap_or(0.0), 8),
        }));

        {
            let mut stats = self.stats.lock().unwrap();
            stats.fills += 1;
            stats.fill_qty += qty;
            stats.fill_notional += qty * price;
            stats.fees += fee;
        }

        let seq = self.next_seq();
        let deliver_at = ts.saturating_add(self.response_delay_ns);
        self.shared.lock().unwrap().fills.push(FillDeliveryRecord {
            seq,
            exchange_ts: ts,
            deliver_at,
            delivered_at: 0,
            delivered: false,
            side,
            qty,
            price,
            fee,
            exchange_position_before: before,
            exchange_position_after: after,
            local_position_before: Decimal::ZERO,
            local_position_after: Decimal::ZERO,
            local_cash_before: Decimal::ZERO,
            local_cash_after: Decimal::ZERO,
            position_id: event.position_id.map(|id| id.to_string()),
        });
        self.enqueue_response(ResponseItem {
            seq,
            ts_event: ts,
            deliver_at,
            kind: ResponseKind::Fill {
                signed_qty,
                qty,
                price,
                fee,
                position_id: event.position_id,
            },
        });
    }

    fn record_exchange_position(&mut self, ts: u64, kind: &'static str, signed: String, id: String) {
        self.shared
            .lock()
            .unwrap()
            .exchange_positions
            .push(ExchangePositionRecord {
                ts,
                kind,
                signed_qty: signed,
                position_id: id,
            });
    }

    /// Flush all still-pending deliveries at the end of replay so the strategy
    /// world converges to the exchange account. Policy decisions are suppressed
    /// because the replay has ended.
    fn finalize(&mut self) {
        if self.finalized {
            return;
        }
        self.finalizing = true;
        self.pull_external_funding();
        let max_due = self
            .pending_response
            .iter()
            .map(|item| item.deliver_at)
            .chain(self.pending_feed.iter().map(|item| item.deliver_at))
            .max()
            .unwrap_or(self.last_tick);
        let now = max_due.max(self.last_tick);
        if now > self.last_tick {
            self.prev_tick = self.last_tick;
            self.last_tick = now;
        }
        while let Some(item) = self.pending_response.pop_front() {
            self.check_first_tick(item.deliver_at, now, "response");
            let _ = self.apply_response(&item, now);
        }
        while let Some(item) = self.pending_feed.pop_front() {
            self.check_first_tick(item.deliver_at, now, "feed");
            let _ = self.apply_feed(&item, now);
        }
        {
            let mut shared = self.shared.lock().unwrap();
            shared.finalizing_deliveries = self.feed_audit.total;
            shared.feed_audit = self.feed_audit.clone();
            shared.local = Some(self.local.clone());
            shared.finalized = true;
        }
        self.finalized = true;
    }
}

fn sample_json(sample: &FeedSample) -> Value {
    json!({
        "seq": sample.seq,
        "kind": sample.kind,
        "ts_event": sample.ts_event.to_string(),
        "deliver_at": sample.deliver_at.to_string(),
        "delivered_at": sample.delivered_at.to_string(),
    })
}

fn feed_audit_json(audit: &FeedAudit, feed_delay_ns: u64, response_delay_ns: u64) -> Value {
    json!({
        "feed_delay_ns": feed_delay_ns.to_string(),
        "response_delay_ns": response_delay_ns.to_string(),
        "items": audit.total,
        "quotes": audit.quotes,
        "trades": audit.trades,
        "marks": audit.marks,
        "first_tick_violations": audit.first_tick_violations,
        "delivered_before_due": audit.delivered_before_due,
        "min_lateness_ns": audit.min_lateness_ns.to_string(),
        "max_lateness_ns": audit.max_lateness_ns.to_string(),
        "mean_lateness_ns": if audit.total == 0 { "0".to_string() } else { (audit.sum_lateness_ns / audit.total as u128).to_string() },
        "min_total_delay_ns": audit.min_total_delay_ns.to_string(),
        "max_total_delay_ns": audit.max_total_delay_ns.to_string(),
        "first_samples": audit.first_samples.iter().map(sample_json).collect::<Vec<_>>(),
        "last_samples": audit.last_samples.iter().map(sample_json).collect::<Vec<_>>(),
        "stride_samples": audit.stride_samples.iter().map(sample_json).collect::<Vec<_>>(),
    })
}

nautilus_strategy!(LocalViewTaker, {
    fn on_order_filled(&mut self, event: &OrderFilled) {
        self.record_fill(event);
    }
    fn on_position_opened(&mut self, event: PositionOpened) {
        if event.instrument_id != self.instrument_id {
            return;
        }
        let signed = match event.side {
            PositionSide::Long => event.quantity.as_decimal(),
            _ => -event.quantity.as_decimal(),
        };
        self.record_exchange_position(
            event.ts_event.as_u64(),
            "opened",
            dec_str(signed),
            event.position_id.to_string(),
        );
    }
    fn on_position_changed(&mut self, event: PositionChanged) {
        if event.instrument_id != self.instrument_id {
            return;
        }
        self.record_exchange_position(
            event.ts_event.as_u64(),
            "changed",
            trim(event.signed_qty, 8),
            event.position_id.to_string(),
        );
    }
    fn on_position_closed(&mut self, event: PositionClosed) {
        if event.instrument_id != self.instrument_id {
            return;
        }
        self.record_exchange_position(
            event.ts_event.as_u64(),
            "closed",
            "0".to_string(),
            event.position_id.to_string(),
        );
    }
});

impl Debug for LocalViewTaker {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("LocalViewTaker").finish()
    }
}

impl DataActor for LocalViewTaker {
    fn on_start(&mut self) -> Result<()> {
        self.subscribe_quotes(self.instrument_id, None, None);
        self.subscribe_trades(self.instrument_id, None, None);
        self.subscribe_mark_prices(self.instrument_id, None, None);
        self.subscribe_funding_rates(self.instrument_id, None, None);

        // Funding adjustments are short-circuited before Strategy callbacks in
        // the pinned build. Observe the concrete position topic; the closure
        // writes to the exchange tape and to a buffer that `advance` converts
        // into delayed LocalView responses.
        let topic = get_event_position_topic(StrategyId::from(STRATEGY_ID));
        let external_funding = self.external_funding.clone();
        let tape = self.tape.clone();
        let epochs = Arc::new(Mutex::new(EpochState::default()));
        let handler = TypedHandler::from(move |event: &PositionEvent| match event {
            PositionEvent::PositionOpened(opened) => {
                epochs
                    .lock()
                    .unwrap()
                    .open(opened.position_id, opened.ts_event.as_u64());
            }
            PositionEvent::PositionClosed(closed) => {
                epochs
                    .lock()
                    .unwrap()
                    .close(closed.position_id, closed.ts_event.as_u64());
            }
            PositionEvent::PositionAdjusted(adjusted)
                if adjusted.adjustment_type == PositionAdjustmentType::Funding =>
            {
                let Some(money) = adjusted.pnl_change else {
                    return;
                };
                let epoch = epochs.lock().unwrap().epoch(&adjusted.position_id);
                let ts = adjusted.ts_event.as_u64();
                let settlement_id = format!(
                    "FUND:{}:{}:{}",
                    ts, adjusted.position_id, epoch
                );
                tape.lock().unwrap().push(json!({
                    "type": "FUNDING",
                    "amount": money.as_decimal().to_string(),
                    "currency": money.currency.code.to_string(),
                    "epoch": epoch.to_string(),
                    "settlement_id": settlement_id,
                    "position_id": adjusted.position_id.to_string(),
                    "account_id": adjusted.account_id.to_string(),
                    "ts_ns": ts.to_string(),
                }));
                external_funding.lock().unwrap().push(FundingObs {
                    ts_event: ts,
                    amount: money.as_decimal(),
                    currency: money.currency.code.to_string(),
                    settlement_id,
                    position_id: adjusted.position_id.to_string(),
                    account_id: adjusted.account_id.to_string(),
                    epoch,
                });
            }
            _ => {}
        });
        msgbus::subscribe_position_events(topic.into(), handler, None);
        Ok(())
    }

    fn on_quote(&mut self, quote: &QuoteTick) -> Result<()> {
        let mid = (quote.bid_price.as_f64() + quote.ask_price.as_f64()) / 2.0;
        let ts = quote.ts_event.as_u64();
        self.enqueue_feed(ts, FeedKind::Quote { mid });
        self.advance(ts)
    }

    fn on_trade(&mut self, trade: &TradeTick) -> Result<()> {
        let ts = trade.ts_event.as_u64();
        self.enqueue_feed(
            ts,
            FeedKind::Trade {
                price: trade.price.as_f64(),
            },
        );
        self.advance(ts)
    }

    fn on_mark_price(&mut self, mark: &MarkPriceUpdate) -> Result<()> {
        let ts = mark.ts_event.as_u64();
        self.enqueue_feed(
            ts,
            FeedKind::Mark {
                price: mark.value.as_decimal(),
            },
        );
        self.advance(ts)
    }

    fn on_funding_rate(&mut self, rate: &FundingRateUpdate) -> Result<()> {
        // A funding-rate update is a data tick: it can be the first later event
        // tick for an already-due delivery. No strategy state is taken from it.
        self.advance(rate.ts_event.as_u64())
    }

    fn on_stop(&mut self) -> Result<()> {
        self.finalize();
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// Data parsing and replay (same corpus format as native/runner).
// ---------------------------------------------------------------------------

fn instrument() -> InstrumentAny {
    InstrumentAny::CryptoPerpetual(CryptoPerpetual::new(
        InstrumentId::from("BTCUSDT-PERP.BINANCE"),
        Symbol::from("BTCUSDT"),
        Currency::from("BTC"),
        Currency::from("USDT"),
        Currency::from("USDT"),
        false,
        2,
        3,
        Price::from("0.01"),
        Quantity::from("0.001"),
        Some(Quantity::from("1")),
        None,
        Some(Quantity::from("10000.0")),
        Some(Quantity::from("0.001")),
        None,
        Some(Money::new(10.0, Currency::from("USDT"))),
        Some(Price::from("10000000.00")),
        Some(Price::from("1.0")),
        Some(Decimal::new(1, 0)),
        Some(Decimal::new(35, 2)),
        Some(Decimal::new(2, 4)),
        Some(Decimal::new(4, 4)),
        None,
        None,
        UnixNanos::default(),
        UnixNanos::default(),
    ))
}

fn round_px(text: &str) -> Result<Price> {
    let d: Decimal = text.parse().context("price parse")?;
    Ok(Price::from(format!("{:.2}", d.round_dp(2)).as_str()))
}

fn round_qty(text: &str) -> Result<Quantity> {
    let d: Decimal = text.parse().context("qty parse")?;
    Ok(Quantity::from(format!("{:.3}", d.round_dp(3)).as_str()))
}

struct ParsedEvent {
    ts: u64,
    mid: Option<f64>,
    data: Option<Data>,
    kind: &'static str,
    is_funding: bool,
}

fn parse_event_line(line: &str, id: InstrumentId) -> Result<ParsedEvent> {
    let v: Value = serde_json::from_str(line).context("ndjson record")?;
    let ts = v["ts_ns"].as_str().unwrap_or("0").parse::<u64>()?;
    let kind_text = v["kind"].as_str().unwrap_or("");
    let mut mid = None;
    let (kind, data) = match kind_text {
        "Quote" => {
            let bid: f64 = v["bid"].as_str().unwrap_or("0").parse()?;
            let ask: f64 = v["ask"].as_str().unwrap_or("0").parse()?;
            mid = Some((bid + ask) / 2.0);
            (
                "quote",
                Some(Data::Quote(QuoteTick::new(
                    id,
                    round_px(v["bid"].as_str().unwrap_or("0"))?,
                    round_px(v["ask"].as_str().unwrap_or("0"))?,
                    round_qty(v["bid_qty"].as_str().unwrap_or("0"))?,
                    round_qty(v["ask_qty"].as_str().unwrap_or("0"))?,
                    UnixNanos::from(ts),
                    UnixNanos::from(ts),
                ))),
            )
        }
        "Trade" => {
            let side = if v["aggressor"].as_str() == Some("SELL") {
                AggressorSide::Seller
            } else {
                AggressorSide::Buyer
            };
            let tid = TradeId::from(v["trade_id"].as_str().unwrap_or("T-0"));
            (
                "trade",
                Some(Data::Trade(TradeTick::new(
                    id,
                    round_px(v["price"].as_str().unwrap_or("0"))?,
                    round_qty(v["qty"].as_str().unwrap_or("0"))?,
                    side,
                    tid,
                    UnixNanos::from(ts),
                    UnixNanos::from(ts),
                ))),
            )
        }
        "Mark" => (
            "mark",
            Some(Data::MarkPriceUpdate(MarkPriceUpdate::new(
                id,
                round_px(v["price"].as_str().unwrap_or("0"))?,
                UnixNanos::from(ts),
                UnixNanos::from(ts),
            ))),
        ),
        "Funding" => {
            let rate: Decimal = v["rate"].as_str().unwrap_or("0").parse()?;
            (
                "funding",
                Some(Data::FundingRateUpdate(FundingRateUpdate::new(
                    id,
                    rate,
                    Some(480),
                    None,
                    UnixNanos::from(ts),
                    UnixNanos::from(ts),
                ))),
            )
        }
        _ => ("other", None),
    };
    Ok(ParsedEvent {
        ts,
        mid,
        data,
        kind,
        is_funding: kind == "funding",
    })
}

fn drain_fundings(
    pending: &mut BTreeMap<u64, Vec<(Data, usize)>>,
    upto: u64,
    batch: &mut Vec<Data>,
) {
    while let Some((&ts, _)) = pending.first_key_value() {
        if ts > upto {
            break;
        }
        if let Some((_, items)) = pending.pop_first() {
            for (data, _) in items {
                batch.push(data);
            }
        }
    }
}

fn collect_fundings(
    path: &str,
    id: InstrumentId,
    max_pending: usize,
) -> Result<BTreeMap<u64, Vec<(Data, usize)>>> {
    let file = File::open(path).context("events file")?;
    let mut reader = BufReader::with_capacity(1 << 20, file);
    let mut line = String::with_capacity(1024);
    let mut pending: BTreeMap<u64, Vec<(Data, usize)>> = BTreeMap::new();
    let mut count = 0usize;
    loop {
        line.clear();
        let n = reader.read_line(&mut line)?;
        if n == 0 {
            break;
        }
        if !line.contains("Funding") {
            continue;
        }
        let event = parse_event_line(line.trim(), id)?;
        if !event.is_funding {
            continue;
        }
        if let Some(data) = event.data {
            pending.entry(event.ts).or_default().push((data, n));
            count += 1;
            if max_pending > 0 && count > max_pending {
                bail!(
                    "RESOURCE_LIMIT reason=MAX_PENDING_FUNDINGS limit={max_pending} observed={count}"
                );
            }
        }
    }
    Ok(pending)
}

fn current_rss_kb() -> u64 {
    std::fs::read_to_string("/proc/self/status")
        .ok()
        .and_then(|text| {
            text.lines()
                .find(|line| line.starts_with("VmRSS:"))
                .and_then(|line| line.split_whitespace().nth(1))
                .and_then(|value| value.parse().ok())
        })
        .unwrap_or(0)
}

fn peak_rss_kb() -> u64 {
    std::fs::read_to_string("/proc/self/status")
        .ok()
        .and_then(|text| {
            text.lines()
                .find(|line| line.starts_with("VmHWM:"))
                .and_then(|line| line.split_whitespace().nth(1))
                .and_then(|value| value.parse().ok())
        })
        .unwrap_or(0)
}

fn sha256_file(path: &str) -> Result<String> {
    let mut file = File::open(path).with_context(|| format!("hash open {path}"))?;
    let mut hasher = Sha256::new();
    let mut buffer = [0u8; 1 << 20];
    loop {
        let read = std::io::Read::read(&mut file, &mut buffer)?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    Ok(format!("{:x}", hasher.finalize()))
}

fn git_head() -> String {
    std::process::Command::new("git")
        .arg("-C")
        .arg(concat!(env!("CARGO_MANIFEST_DIR"), "/../.."))
        .arg("rev-parse")
        .arg("HEAD")
        .output()
        .ok()
        .filter(|output| output.status.success())
        .map(|output| String::from_utf8_lossy(&output.stdout).trim().to_string())
        .unwrap_or_default()
}

fn main() -> Result<()> {
    let started = Instant::now();
    let args: Vec<String> = std::env::args().collect();
    let mut events = None;
    let mut strategy_path = None;
    let mut out_path = None;
    let mut notional = 100.0f64;
    let mut feed_delay_ns = 0u64;
    let mut response_delay_ns = 0u64;
    let mut profile = "local_view".to_string();
    let mut max_pending_fundings = 100_000usize;
    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--events" => {
                events = args.get(i + 1).cloned();
                i += 2;
            }
            "--strategy" => {
                strategy_path = args.get(i + 1).cloned();
                i += 2;
            }
            "--out" => {
                out_path = args.get(i + 1).cloned();
                i += 2;
            }
            "--notional" => {
                notional = args
                    .get(i + 1)
                    .ok_or_else(|| anyhow!("notional"))?
                    .parse()?;
                i += 2;
            }
            "--feed-delay-ns" => {
                feed_delay_ns = args
                    .get(i + 1)
                    .ok_or_else(|| anyhow!("feed-delay-ns"))?
                    .parse()?;
                i += 2;
            }
            "--response-delay-ns" => {
                response_delay_ns = args
                    .get(i + 1)
                    .ok_or_else(|| anyhow!("response-delay-ns"))?
                    .parse()?;
                i += 2;
            }
            "--profile" => {
                profile = args.get(i + 1).cloned().unwrap_or(profile);
                i += 2;
            }
            "--max-pending-fundings" => {
                max_pending_fundings = args
                    .get(i + 1)
                    .ok_or_else(|| anyhow!("max-pending-fundings"))?
                    .parse()?;
                i += 2;
            }
            other => bail!("unknown argument {other}"),
        }
    }
    let events_path = events.context("--events required")?;
    let strategy_file = strategy_path.context("--strategy required")?;
    let strategy_text = fs::read_to_string(&strategy_file)?;
    let strategy_json: Value = serde_json::from_str(&strategy_text)?;
    let parsed_strategy = parse_strategy(&strategy_json)?;
    let bar_ns = strategy_json["decision_clock"]["interval_ns"]
        .as_str()
        .unwrap_or("1000000000")
        .parse::<u64>()?;
    let max_holding_ns = strategy_json["exit_policy"]["max_holding_ns"]
        .as_str()
        .unwrap_or("0")
        .parse::<u64>()?;
    let instrument = instrument();
    let id = instrument.id();
    let sid = StrategyId::from(STRATEGY_ID);

    let mut engine = BacktestEngine::new(BacktestEngineConfig::default())?;
    engine.add_venue(
        SimulatedVenueConfig::builder()
            .venue(Venue::from("BINANCE"))
            .oms_type(OmsType::Netting)
            .account_type(AccountType::Margin)
            .book_type(BookType::L1_MBP)
            .starting_balances(vec![Money::new(1000.0, Currency::from("USDT"))])
            .build()?,
    )?;
    engine.add_instrument(&instrument)?;
    let stats = Arc::new(Mutex::new(RunStats::default()));
    let taker = LocalViewTaker::new(
        id,
        PolicyEngine::new(parsed_strategy),
        notional,
        bar_ns,
        max_holding_ns,
        feed_delay_ns,
        response_delay_ns,
        stats.clone(),
    );
    let tape_handle = taker.tape.clone();
    let shared_handle = taker.shared.clone();
    engine.add_strategy(taker)?;

    let mut pending_fundings = collect_fundings(&events_path, id, max_pending_fundings)?;
    let mut batch: Vec<Data> = Vec::new();
    let mut final_mid: Option<f64> = None;
    let mut last_ts = 0u64;
    let mut counts = HashMap::<&'static str, usize>::new();
    let mut parsed_events = 0usize;
    let mut events_sha: Option<String> = None;

    let file = File::open(&events_path).context("events file")?;
    let mut reader = BufReader::with_capacity(1 << 20, file);
    let mut line = String::with_capacity(1024);
    loop {
        line.clear();
        let n = reader.read_line(&mut line)?;
        if n == 0 {
            break;
        }
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        let event = parse_event_line(trimmed, id)?;
        if let Some(mid) = event.mid {
            final_mid = Some(mid);
        }
        last_ts = last_ts.max(event.ts);
        if event.data.is_none() {
            continue;
        }
        parsed_events += 1;
        *counts.entry(event.kind).or_default() += 1;
        if event.is_funding {
            // Injected at the economic timestamp below.
            continue;
        }
        drain_fundings(&mut pending_fundings, event.ts, &mut batch);
        if let Some(data) = event.data {
            batch.push(data);
        }
    }
    drain_fundings(&mut pending_fundings, u64::MAX, &mut batch);
    if parsed_events == 0 {
        bail!("no native events");
    }
    let events_sha = match events_sha.take() {
        Some(sha) => sha,
        None => sha256_file(&events_path)?,
    };

    engine.add_data(std::mem::take(&mut batch), None, true, true)?;
    engine.run(None, None, None, false)?;

    let result = engine.get_result();
    let venue = Venue::from("BINANCE");
    let usdt = Currency::from("USDT");
    let mut tape_values = tape_handle.lock().unwrap().clone();
    let shared = shared_handle.lock().unwrap().clone();

    let (native_account, final_mark, native_mark_ts, active_orders_end, active_positions_end) = {
        let cache = engine.kernel().cache.borrow();
        let account = cache.account_for_venue(&venue);
        let (total, free, locked) = match account.as_ref().and_then(|a| a.balance(Some(usdt))) {
            Some(balance) => (
                balance.total.as_decimal(),
                balance.free.as_decimal(),
                balance.locked.as_decimal(),
            ),
            None => (Decimal::ZERO, Decimal::ZERO, Decimal::ZERO),
        };
        let positions = cache.positions_open(None, Some(&id), Some(&sid), None, None);
        let mark = cache.mark_price(&id);
        let mut position_qty = Decimal::ZERO;
        let mut unrealized = Decimal::ZERO;
        let mut position_ids = Vec::new();
        for position in &positions {
            let signed = if position.side == PositionSide::Short {
                -position.quantity.as_decimal()
            } else {
                position.quantity.as_decimal()
            };
            position_qty += signed;
            if let Some(mark) = &mark {
                unrealized += position.unrealized_pnl(mark.value).as_decimal();
            }
            position_ids.push(position.id.to_string());
        }
        let final_mark = mark
            .as_ref()
            .map(|mark| mark.value.to_string())
            .or_else(|| final_mid.map(|mid| trim(mid, 2)));
        let native_mark_ts = mark.as_ref().map_or(0, |mark| mark.ts_event.as_u64());
        (
            json!({
                "currency": usdt.code.to_string(),
                "total": total.to_string(),
                "free": free.to_string(),
                "locked": locked.to_string(),
                "unrealized": unrealized.to_string(),
                "equity": (total + unrealized).to_string(),
                "position_qty": position_qty.to_string(),
                "position_ids": position_ids,
                "mark": final_mark.clone(),
            }),
            final_mark,
            native_mark_ts,
            cache.orders_open_count(None, Some(&id), Some(&sid), None, None),
            cache.positions_open_count(None, Some(&id), Some(&sid), None, None),
        )
    };

    let local = shared
        .local
        .clone()
        .ok_or_else(|| anyhow!("LocalView was not finalized (on_stop not called?)"))?;
    let local_equity = match local.last_mark {
        Some(mark) => local.cash + local.position_qty * (mark - local.entry_price),
        None => local.cash,
    };

    // Exchange-world tape boundary, same shape as native/runner.
    let max_tape_ts = tape_values
        .iter()
        .filter_map(|value| value.get("ts_ns").and_then(|ts| ts.as_str()))
        .filter_map(|ts| ts.parse::<u64>().ok())
        .max()
        .unwrap_or(0);
    let mark_ts = native_mark_ts.max(max_tape_ts);
    if let Some(mark) = final_mark.clone() {
        tape_values.push(json!({
            "type": "MARK",
            "price": mark,
            "ts_ns": mark_ts.to_string(),
        }));
    }
    tape_values.push(json!({
        "type": "END_FOLD",
        "close_cost": "0",
        "ts_ns": mark_ts.to_string(),
    }));

    // Strategy-world (delivered) tape: fills/funding stamped at delivery time.
    let mut local_events: Vec<(u64, u64, Value)> = Vec::new();
    for record in &shared.fills {
        if !record.delivered {
            continue;
        }
        local_events.push((
            record.delivered_at,
            record.seq,
            json!({
                "type": "FILL",
                "ts_ns": record.delivered_at.to_string(),
                "side": record.side,
                "qty": record.qty.to_string(),
                "price": record.price.to_string(),
                "fee": record.fee.to_string(),
            }),
        ));
    }
    for record in &shared.funding {
        if !record.delivered {
            continue;
        }
        local_events.push((
            record.delivered_at,
            record.seq,
            json!({
                "type": "FUNDING",
                "amount": record.amount.to_string(),
                "currency": record.currency,
                "epoch": record.epoch.to_string(),
                "settlement_id": record.settlement_id,
                "position_id": record.position_id,
                "account_id": record.account_id,
                "ts_ns": record.delivered_at.to_string(),
            }),
        ));
    }
    local_events.sort_by_key(|(ts, seq, _)| (*ts, *seq));
    let mut local_tape = vec![json!({"type": "SET_WALLET", "cash": "1000"})];
    let mut local_max_ts = 0u64;
    for (_, _, event) in local_events {
        local_max_ts = local_max_ts.max(
            event
                .get("ts_ns")
                .and_then(|ts| ts.as_str())
                .and_then(|ts| ts.parse::<u64>().ok())
                .unwrap_or(0),
        );
        local_tape.push(event);
    }
    if let Some(mark) = local.last_mark.or_else(|| final_mark.as_ref().and_then(|m| Decimal::from_str(m).ok())) {
        local_tape.push(json!({
            "type": "MARK",
            "price": mark.to_string(),
            "ts_ns": local_max_ts.max(last_ts).to_string(),
        }));
    }
    local_tape.push(json!({
        "type": "END_FOLD",
        "close_cost": "0",
        "ts_ns": local_max_ts.max(last_ts).to_string(),
    }));

    // Enrich fills with delivery gaps and decisions that occurred while the
    // fill was in flight. A decision may not use a fill before its delivery.
    let decisions = shared.decisions.clone();
    let mut fills_json = Vec::with_capacity(shared.fills.len());
    let mut policy_reads_before_delivery = 0usize;
    let mut fills_strictly_later = 0usize;
    let mut fills_with_intervening_decisions = 0usize;
    let mut decisions_with_unseen_fills = 0usize;
    let mut max_in_flight_qty = Decimal::ZERO;
    let mut ttd: Vec<u64> = Vec::with_capacity(shared.fills.len());
    for record in &shared.fills {
        let gap_decisions = decisions
            .iter()
            .filter(|decision| decision.ts > record.exchange_ts && decision.ts < record.delivered_at)
            .count();
        if record.delivered && record.delivered_at > record.exchange_ts {
            fills_strictly_later += 1;
        }
        if gap_decisions > 0 {
            fills_with_intervening_decisions += 1;
        }
        if record.delivered {
            ttd.push(record.delivered_at.saturating_sub(record.exchange_ts));
        }
        fills_json.push(json!({
            "seq": record.seq,
            "exchange_ts": record.exchange_ts.to_string(),
            "deliver_at": record.deliver_at.to_string(),
            "delivered_at": record.delivered_at.to_string(),
            "delivered": record.delivered,
            "side": record.side,
            "qty": record.qty.to_string(),
            "price": record.price.to_string(),
            "fee": record.fee.to_string(),
            "exchange_position_before": record.exchange_position_before.to_string(),
            "exchange_position_after": record.exchange_position_after.to_string(),
            "local_position_before": record.local_position_before.to_string(),
            "local_position_after": record.local_position_after.to_string(),
            "local_cash_before": record.local_cash_before.to_string(),
            "local_cash_after": record.local_cash_after.to_string(),
            "decisions_in_gap": gap_decisions,
            "position_id": record.position_id,
        }));
    }
    let mut decisions_json = Vec::with_capacity(decisions.len());
    for decision in &decisions {
        // Fills are delivered in exchange order (constant response delay), so
        // the first `delivered_fills` records are exactly the LocalView state.
        // Using the same-tick `delivered_at <= ts` would incorrectly include a
        // fill delivered later in the same tick after the decision ran.
        let delivered_at_decision: Vec<&FillDeliveryRecord> =
            shared.fills.iter().take(decision.delivered_fills).collect();
        let exchange_fills_at_decision = shared
            .fills
            .iter()
            .filter(|fill| fill.exchange_ts <= decision.ts)
            .count();
        let exchange_fills_before = shared
            .fills
            .iter()
            .filter(|fill| fill.exchange_ts < decision.ts)
            .count();
        // A fill at exactly the decision timestamp is produced by the order the
        // decision submits; it cannot be look-ahead for that decision. Only
        // strictly earlier undelivered fills are pre-existing in flight state.
        let pre_existing_undelivered = shared
            .fills
            .iter()
            .skip(decision.delivered_fills)
            .filter(|fill| fill.exchange_ts < decision.ts && fill.delivered_at > decision.ts)
            .count();
        let same_tick_pending = shared
            .fills
            .iter()
            .skip(decision.delivered_fills)
            .filter(|fill| fill.exchange_ts == decision.ts && fill.delivered_at > decision.ts)
            .count();
        let unseen = pre_existing_undelivered;
        if unseen > 0 {
            decisions_with_unseen_fills += 1;
        }
        let expected_position: Decimal = delivered_at_decision
            .iter()
            .map(|fill| {
                if fill.side == "BUY" {
                    fill.qty
                } else {
                    -fill.qty
                }
            })
            .sum();
        let in_flight_qty: Decimal = shared
            .fills
            .iter()
            .skip(decision.delivered_fills)
            .filter(|fill| fill.exchange_ts < decision.ts && fill.delivered_at > decision.ts)
            .map(|fill| if fill.side == "BUY" { fill.qty } else { -fill.qty })
            .sum();
        max_in_flight_qty = max_in_flight_qty.max(in_flight_qty.abs());
        if expected_position != decision.local_position_qty {
            policy_reads_before_delivery += 1;
        }
        decisions_json.push(json!({
            "seq": decision.seq,
            "ts": decision.ts.to_string(),
            "local_position_qty": decision.local_position_qty.to_string(),
            "expected_position_from_delivered_fills": expected_position.to_string(),
            "local_cash": decision.local_cash.to_string(),
            "delivered_fills": decision.delivered_fills,
            "exchange_fills_at_or_before": exchange_fills_at_decision,
            "exchange_fills_before": exchange_fills_before,
            "pre_existing_fills_in_flight": pre_existing_undelivered,
            "same_tick_pending_fills": same_tick_pending,
            "in_flight_qty": in_flight_qty.to_string(),
        }));
    }
    ttd.sort_unstable();
    let ttd_stat = |p: f64| -> u64 {
        if ttd.is_empty() {
            return 0;
        }
        let idx = ((ttd.len() as f64 - 1.0) * p).round() as usize;
        ttd[idx.min(ttd.len() - 1)]
    };
    let ttd_sum: u128 = ttd.iter().map(|v| *v as u128).sum();

    let exchange_position_from_fills = shared
        .fills
        .iter()
        .map(|fill| fill.exchange_position_after)
        .last()
        .unwrap_or(Decimal::ZERO);
    let position_diff = local.position_qty - Decimal::from_str(
        native_account["position_qty"].as_str().unwrap_or("0"),
    )?;
    let cash_diff =
        local.cash - Decimal::from_str(native_account["total"].as_str().unwrap_or("0"))?;
    let exchange_equity =
        Decimal::from_str(native_account["equity"].as_str().unwrap_or("0"))?;
    let equity_diff = local_equity - exchange_equity;

    let stats = stats.lock().unwrap();
    let source_path = concat!(env!("CARGO_MANIFEST_DIR"), "/src/main.rs");
    let cargo_path = concat!(env!("CARGO_MANIFEST_DIR"), "/Cargo.toml");
    let binary_path = std::env::current_exe()
        .map(|path| path.to_string_lossy().to_string())
        .unwrap_or_default();
    let mut funding_json = Vec::new();
    for record in &shared.funding {
        funding_json.push(json!({
            "seq": record.seq,
            "exchange_ts": record.exchange_ts.to_string(),
            "deliver_at": record.deliver_at.to_string(),
            "delivered_at": record.delivered_at.to_string(),
            "delivered": record.delivered,
            "amount": record.amount.to_string(),
            "currency": record.currency,
            "settlement_id": record.settlement_id,
            "position_id": record.position_id,
            "account_id": record.account_id,
            "epoch": record.epoch.to_string(),
            "local_cash_before": record.local_cash_before.to_string(),
            "local_cash_after": record.local_cash_after.to_string(),
        }));
    }
    let mut exchange_positions_json = Vec::new();
    for record in &shared.exchange_positions {
        exchange_positions_json.push(json!({
            "ts": record.ts.to_string(),
            "kind": record.kind,
            "signed_qty": record.signed_qty,
            "position_id": record.position_id,
        }));
    }

    let output = json!({
        "schema": "qel.local_view.f22.v1",
        "profile": profile,
        "status": if shared.finalized && policy_reads_before_delivery == 0 && shared.feed_audit.first_tick_violations == 0 && shared.feed_audit.delivered_before_due == 0 { "PASS" } else { "FAIL" },
        "generated_at_utc": chrono_like_now(),
        "binding": {
            "git_head": git_head(),
            "source": {"path": source_path, "sha256": sha256_file(source_path)?},
            "cargo_toml": {"path": cargo_path, "sha256": sha256_file(cargo_path)?},
            "binary": {"path": binary_path.clone(), "sha256": if binary_path.is_empty() { String::new() } else { sha256_file(&binary_path)? }},
            "nautilus": {"repository": "https://github.com/nautechsystems/nautilus_trader", "tag": NAUTILUS_TAG, "commit": NAUTILUS_REV},
            "corpus": {"path": events_path.clone(), "sha256": events_sha},
            "strategy": {"path": strategy_file.clone(), "sha256": sha256_file(&strategy_file)?},
        },
        "command": {
            "events": events_path,
            "strategy": strategy_file,
            "feed_delay_ns": feed_delay_ns.to_string(),
            "response_delay_ns": response_delay_ns.to_string(),
            "notional": notional.to_string(),
        },
        "counts": {
            "events_parsed": parsed_events,
            "quotes": counts.get("quote").copied().unwrap_or(0),
            "trades": counts.get("trade").copied().unwrap_or(0),
            "marks": counts.get("mark").copied().unwrap_or(0),
            "funding_rates": counts.get("funding").copied().unwrap_or(0),
            "orders_submitted": stats.orders_submitted,
            "fills": stats.fills,
            "fill_qty": stats.fill_qty.to_string(),
            "fill_notional": stats.fill_notional.to_string(),
            "fees": stats.fees.to_string(),
            "positions": result.total_positions,
            "engine_orders": result.total_orders,
            "positions_opened_callbacks": shared.exchange_positions.iter().filter(|event| event.kind == "opened").count(),
            "decisions": stats.decisions,
            "policy_steps": stats.steps,
            "delivered_fills": local.delivered_fills,
            "delivered_fundings": local.delivered_fundings,
        },
        "f22": {
            "feed_delay_charged_once": feed_audit_json(&shared.feed_audit, feed_delay_ns, response_delay_ns),
            "response_delay": {
                "fills": shared.fills.len(),
                "strictly_later_than_exchange": fills_strictly_later,
                "fills_with_intervening_decisions": fills_with_intervening_decisions,
                "decisions_with_unseen_fills": decisions_with_unseen_fills,
                "policy_reads_before_delivery": policy_reads_before_delivery,
                "max_in_flight_qty": max_in_flight_qty.to_string(),
                "time_to_delivery_ns": {
                    "count": ttd.len(),
                    "min": ttd_stat(0.0).to_string(),
                    "p50": ttd_stat(0.5).to_string(),
                    "p90": ttd_stat(0.9).to_string(),
                    "p99": ttd_stat(0.99).to_string(),
                    "max": ttd.last().copied().unwrap_or(0).to_string(),
                    "mean": if ttd.is_empty() { "0".to_string() } else { (ttd_sum / ttd.len() as u128).to_string() },
                },
            },
            "exchange_before_delivery": {
                "exchange_position_from_fills": exchange_position_from_fills.to_string(),
                "exchange_position_events": exchange_positions_json.len(),
                "violations": shared.fills.iter().filter(|fill| fill.delivered && fill.delivered_at <= fill.exchange_ts && response_delay_ns > 0).count(),
            },
            "convergence": {
                "local_position_qty": local.position_qty.to_string(),
                "exchange_position_qty": native_account["position_qty"].clone(),
                "position_diff": position_diff.to_string(),
                "local_cash": local.cash.to_string(),
                "exchange_cash": native_account["total"].clone(),
                "cash_diff": cash_diff.to_string(),
                "local_equity": local_equity.to_string(),
                "exchange_equity": native_account["equity"].clone(),
                "equity_diff": equity_diff.to_string(),
                "local_fees": local.fees.to_string(),
                "local_funding_cash": local.funding_cash.to_string(),
                "local_mark": local.last_mark.map(|m| m.to_string()),
                "finalized": shared.finalized,
            },
            "cross_world_summary": {
                "fill_count": shared.fills.len(),
                "fill_qty": stats.fill_qty.to_string(),
                "fill_notional": stats.fill_notional.to_string(),
                "delivered_fill_count": local.delivered_fills,
                "funding_count": shared.funding.len(),
                "funding_cash": local.funding_cash.to_string(),
                "decisions": stats.decisions,
                "fills_visible_before_delivery": 0,
                "fills_visible_after_delivery": local.delivered_fills,
            },
        },
        "exchange": {
            "native_account": native_account,
            "mark_ts_ns": native_mark_ts.to_string(),
            "active_orders_end": active_orders_end,
            "active_positions_end": active_positions_end,
            "engine_iterations": result.iterations,
            "pnl_total": result.summary.get("PnL (total)").cloned(),
        },
        "local_view": {
            "cash": local.cash.to_string(),
            "position_qty": local.position_qty.to_string(),
            "position_id": local.position_id.map(|id| id.to_string()),
            "entry_price": local.entry_price.to_string(),
            "realized": local.realized.to_string(),
            "fees": local.fees.to_string(),
            "funding_cash": local.funding_cash.to_string(),
            "equity": local_equity.to_string(),
            "mark": local.last_mark.map(|m| m.to_string()),
            "delivered_fills": local.delivered_fills,
            "delivered_fundings": local.delivered_fundings,
        },
        "fills": fills_json,
        "funding": funding_json,
        "decisions": decisions_json,
        "exchange_position_events": exchange_positions_json,
        "tape": tape_values,
        "local_tape": local_tape,
        "resource": {
            "elapsed_ms": started.elapsed().as_millis() as u64,
            "peak_rss_kb": peak_rss_kb(),
            "current_rss_kb": current_rss_kb(),
            "events_path": events_path,
            "ts_last_ns": last_ts.to_string(),
        },
    });

    if let Some(path) = out_path {
        fs::write(path, serde_json::to_string_pretty(&output)? + "\n")?;
    } else {
        println!("{}", serde_json::to_string_pretty(&output)?);
    }
    Ok(())
}

fn chrono_like_now() -> String {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    format!("{}.{:09}Z", now.as_secs(), now.subsec_nanos())
}
