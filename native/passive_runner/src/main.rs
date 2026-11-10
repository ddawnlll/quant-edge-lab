//! QEL passive maker execution probe against pinned NautilusTrader v1.231.0.
//!
//! Scope: issue #10 / TASK-17, limited to the ADR-0003 F02 queue-ahead slice.
//! This binary exercises the public backtest API only. It does not read or
//! modify private engine state and it does not claim any capability beyond the
//! fixtures it actually runs.
//!
//! Output: one JSON document on stdout (and optionally written to argv[1]) with
//! one entry per probe and an explicit PASS / PARTIAL / BLOCKED_BACKEND verdict.

mod adapter;

use std::{
    fmt::Debug,
    fs,
    sync::Mutex,
    time::Instant,
};

use crate::adapter::{Adapter, AggressorView, CancelDecision, Side};
use anyhow::Result;
use nautilus_backtest::{
    config::{BacktestEngineConfig, SimulatedVenueConfig},
    engine::BacktestEngine,
};
use nautilus_common::{actor::DataActor, logging::config::LoggerConfig};
use nautilus_core::UnixNanos;
use nautilus_execution::models::latency::{LatencyModel, StaticLatencyModel};
use nautilus_model::{
    accounts::margin_model::MarginModelAny,
    data::{BookOrder, Data, OrderBookDelta, OrderBookDeltas, QuoteTick, TradeTick},
    enums::{
        AccountType, AggressorSide, BookAction, BookType, ContingencyType, OmsType, OrderSide,
        OrderStatus, OrderType, TimeInForce, TriggerType,
    },
    events::{OrderCancelRejected, OrderEventAny, OrderFilled, OrderRejected, PositionOpened},
    identifiers::{ClientOrderId, InstrumentId, StrategyId, Symbol, TradeId, Venue},
    instruments::{Instrument, InstrumentAny, crypto_perpetual::CryptoPerpetual},
    orderbook::OrderBook,
    orders::{Order, OrderAny},
    types::{Currency, Money, Price, Quantity},
};
use nautilus_trading::{Strategy, StrategyConfig, StrategyCore, nautilus_strategy};
use rust_decimal::Decimal;
use serde_json::{json, Value};

const VENUE: &str = "BINANCE";
const MAINTENANCE_RATE: Decimal = Decimal::from_parts(5, 0, 0, false, 2); // 0.05

// ---------------------------------------------------------------------------
// Probe-side event log (cancel rejects, order rejects, fills)
// ---------------------------------------------------------------------------

static LOG: Mutex<Vec<Value>> = Mutex::new(Vec::new());
static LAST_FILL: Mutex<Option<OrderFilled>> = Mutex::new(None);
/// Last adapter snapshot recorded by the strategy. The adapter is a client
/// order-management layer, so its state is read here rather than from the
/// engine's private internals.
static ADAPTER_STATE: Mutex<Option<Value>> = Mutex::new(None);

fn take_adapter_state() -> Value {
    ADAPTER_STATE
        .lock()
        .ok()
        .and_then(|mut slot| slot.take())
        .unwrap_or(Value::Null)
}

fn adapter_order(state: &Value, client_order_id: &str) -> Value {
    state["orders"]
        .as_array()
        .and_then(|orders| {
            orders
                .iter()
                .find(|order| order["client_order_id"] == client_order_id)
        })
        .cloned()
        .unwrap_or(Value::Null)
}

fn log_event(kind: &str, detail: Value) {
    if let Ok(mut log) = LOG.lock() {
        log.push(json!({"kind": kind, "detail": detail}));
    }
}

fn drain_log() -> Vec<Value> {
    if let Ok(mut last) = LAST_FILL.lock() {
        *last = None;
    }
    LOG.lock()
        .map(|mut log| std::mem::take(&mut *log))
        .unwrap_or_default()
}

fn logged(kind: &str) -> Vec<Value> {
    LOG.lock()
        .map(|log| {
            log.iter()
                .filter(|entry| entry["kind"] == kind)
                .map(|entry| entry["detail"].clone())
                .collect()
        })
        .unwrap_or_default()
}

/// Adapter actions recorded by the strategy, in order.
fn adapter_actions(action: &str) -> Vec<Value> {
    logged("adapter")
        .into_iter()
        .filter(|entry| entry["action"] == action)
        .map(|entry| entry["detail"].clone())
        .collect()
}

fn adapter_detail(action: &str) -> Value {
    adapter_actions(action)
        .into_iter()
        .next()
        .unwrap_or(Value::Null)
}

// ---------------------------------------------------------------------------
// Fixture instrument and data builders
// ---------------------------------------------------------------------------

/// ETHUSDT perpetual, maintenance 0.05 on both sides, zero fees so cash moves
/// are exact. Same construction as the certified F02 spike.
fn instrument() -> InstrumentAny {
    InstrumentAny::CryptoPerpetual(CryptoPerpetual::new(
        InstrumentId::from("ETHUSDT-PERP.BINANCE"),
        Symbol::from("ETHUSDT"),
        Currency::from("ETH"),
        Currency::from("USDT"),
        Currency::from("USDT"),
        false,
        2,
        3,
        Price::from("0.01"),
        Quantity::from("0.001"),
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        Some(MAINTENANCE_RATE),
        Some(MAINTENANCE_RATE),
        Some(Decimal::ZERO),
        Some(Decimal::ZERO),
        None,
        None,
        UnixNanos::default(),
        UnixNanos::default(),
    ))
}

fn ts(n: u64) -> UnixNanos {
    UnixNanos::from(1_000_000_000 + n)
}

fn quote(id: InstrumentId, bid: &str, ask: &str, bid_qty: &str, ask_qty: &str, t: u64) -> Data {
    Data::Quote(QuoteTick::new(
        id,
        Price::from(bid),
        Price::from(ask),
        Quantity::from(bid_qty),
        Quantity::from(ask_qty),
        ts(t),
        ts(t),
    ))
}

fn trade(id: InstrumentId, price: &str, qty: &str, aggressor: AggressorSide, t: u64, trade_id: &str) -> Data {
    Data::Trade(TradeTick::new(
        id,
        Price::from(price),
        Quantity::from(qty),
        aggressor,
        TradeId::new(trade_id),
        ts(t),
        ts(t),
    ))
}

fn delta(
    id: InstrumentId,
    action: BookAction,
    side: OrderSide,
    price: &str,
    qty: &str,
    sequence: u64,
    t: u64,
) -> Data {
    Data::Delta(OrderBookDelta::new(
        id,
        action,
        BookOrder::new(side, Price::from(price), Quantity::from(qty), sequence),
        0,
        sequence,
        ts(t),
        ts(t),
    ))
}

fn new_engine() -> Result<BacktestEngine> {
    BacktestEngine::new(BacktestEngineConfig {
        logging: LoggerConfig {
            bypass_logging: true,
            ..Default::default()
        },
        ..Default::default()
    })
}

fn venue_config(book_type: BookType, queue_position: bool, liquidity_consumption: bool) -> SimulatedVenueConfig {
    venue_config_impl(book_type, queue_position, liquidity_consumption, None)
}

/// Venue config with a deterministic delete (cancel) latency. A cancel command
/// is stamped at `ts_init + delete_latency` and arrives through the exchange
/// inflight queue, which is the public F04 delayed-arrival hook.
fn venue_config_latency(
    book_type: BookType,
    queue_position: bool,
    liquidity_consumption: bool,
    delete_latency_ns: u64,
) -> SimulatedVenueConfig {
    venue_config_impl(
        book_type,
        queue_position,
        liquidity_consumption,
        Some(delete_latency_ns),
    )
}

fn venue_config_impl(
    book_type: BookType,
    queue_position: bool,
    liquidity_consumption: bool,
    delete_latency_ns: Option<u64>,
) -> SimulatedVenueConfig {
    let latency: Option<Box<dyn LatencyModel>> = delete_latency_ns.map(|ns| {
        Box::new(StaticLatencyModel::new(
            UnixNanos::default(),
            UnixNanos::default(),
            UnixNanos::default(),
            UnixNanos::from(ns),
        )) as Box<dyn LatencyModel>
    });
    SimulatedVenueConfig::builder()
        .venue(Venue::from(VENUE))
        .oms_type(OmsType::Netting)
        .account_type(AccountType::Margin)
        .book_type(book_type)
        .starting_balances(vec![Money::from("1000 USDT")])
        .margin_model(MarginModelAny::Standard(StandardMarginModel))
        .trade_execution(true)
        .queue_position(queue_position)
        .liquidity_consumption(liquidity_consumption)
        .support_contingent_orders(true)
        .use_reduce_only(true)
        .reject_stop_orders(true)
        .maybe_latency_model(latency)
        .build()
        .expect("valid simulated venue config")
}

use nautilus_model::accounts::margin_model::StandardMarginModel;

fn run_chunk(engine: &mut BacktestEngine, data: Vec<Data>) -> Result<()> {
    engine.add_data(data, None, true, true)?;
    engine.run(None, None, None, true)?;
    engine.clear_data();
    Ok(())
}

// ---------------------------------------------------------------------------
// Strategy: one passive maker driver, one mode per probe
// ---------------------------------------------------------------------------

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Mode {
    /// F02: resting buy 20 @ 100 with ahead 100, trades 80 then 30.
    F02,
    /// F02 + cancel remainder after the partial fill, then a trade that must not fill.
    F02Cancel,
    /// F02 on an L2 book with a depth reduction after the trade (depth-once).
    F02Depth,
    /// Two passive orders sharing the same price level (shared-level budget).
    TwoLimits,
    /// F03: two marketable IOC orders against a single 10-lot level.
    TwoIoc,
    /// F33 FOK: 6 lots against a 5-lot level.
    Fok,
    /// F33 stop-limit gap: protective sell stop-limit gapped through.
    StopGap,
    /// F04: fill then cancel arrives afterwards.
    CancelRace,
    /// F05: replace increase then decrease.
    Replace,
    /// F34: partial entry then client-managed bracket on the filled quantity.
    PartialBracket,
    /// F06: native OCO bracket, then reduce-only rejection when flat.
    OcoBracket,
    /// F31: partial fill, duplicate fill injected at the OMS, then cancel.
    F31Dup,
    /// Client-managed OCO: both legs are plain orders, so both can fill.
    ClientOcoUnsafe,
    /// F04 over the adapter: local terminal state answers the late cancel.
    AdapterCancelRace,
    /// F05 over the adapter: cancel+new replace with a priority reset count.
    AdapterReplace,
    /// F07 over the adapter: position epoch gate on the protective stop.
    AdapterEpoch,
    /// F31 over the adapter: venue sequence, dedupe and out-of-order ACK.
    AdapterF31,
    /// F06 client profile: atomic client OCO plus a reduce-only exit guard.
    AdapterClientOco,
    /// F02 over the adapter: conservative queue on an unknown aggressor.
    AdapterUnknownAggressor,
}

/// Maps a native aggressor onto the adapter's directional view.
fn aggressor_view(aggressor: AggressorSide) -> AggressorView {
    match aggressor {
        AggressorSide::Buyer => AggressorView::Buyer,
        AggressorSide::Seller => AggressorView::Seller,
        _ => AggressorView::Unknown,
    }
}

/// True for every mode that keeps adapter state.
fn uses_adapter(mode: Mode) -> bool {
    matches!(
        mode,
        Mode::AdapterCancelRace
            | Mode::AdapterReplace
            | Mode::AdapterEpoch
            | Mode::AdapterF31
            | Mode::AdapterClientOco
            | Mode::AdapterUnknownAggressor
    )
}

struct Passive {
    core: StrategyCore,
    instrument_id: InstrumentId,
    mode: Mode,
    quotes: usize,
    trades: usize,
    books: usize,
    oco_submitted: bool,
    reduce_only_probe: bool,
    protection_installed: bool,
    adapter: Adapter,
    last_fill_id: Option<String>,
    replace_id: Option<String>,
}

impl Passive {
    fn new(instrument_id: InstrumentId, mode: Mode, tag: &str) -> Self {
        Self {
            core: StrategyCore::new(StrategyConfig {
                strategy_id: Some(StrategyId::from("PASSIVE-QUEUE-001")),
                order_id_tag: Some(tag.to_string()),
                ..Default::default()
            }),
            instrument_id,
            mode,
            quotes: 0,
            trades: 0,
            books: 0,
            oco_submitted: false,
            reduce_only_probe: false,
            protection_installed: false,
            adapter: Adapter::new(),
            last_fill_id: None,
            replace_id: None,
        }
    }

    fn limit(
        &mut self,
        id: &str,
        side: OrderSide,
        qty: &str,
        price: &str,
        tif: TimeInForce,
        post_only: bool,
        reduce_only: bool,
    ) -> Result<()> {
        let order = self.order().limit(
            self.instrument_id,
            side,
            Quantity::from(qty),
            Price::from(price),
            Some(tif),
            None,
            Some(post_only),
            Some(reduce_only),
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            Some(ClientOrderId::new(id)),
        );
        self.submit_order(order, None, None, None)
    }

    fn market(&mut self, id: &str, side: OrderSide, qty: &str, tif: TimeInForce, reduce_only: bool) -> Result<()> {
        let order = self.order().market(
            self.instrument_id,
            side,
            Quantity::from(qty),
            Some(tif),
            None,
            Some(reduce_only),
            None,
            None,
            None,
            Some(ClientOrderId::new(id)),
        );
        self.submit_order(order, None, None, None)
    }

    fn stop_limit(
        &mut self,
        id: &str,
        side: OrderSide,
        qty: &str,
        price: &str,
        trigger: &str,
        reduce_only: bool,
    ) -> Result<()> {
        let order = self.order().stop_limit(
            self.instrument_id,
            side,
            Quantity::from(qty),
            Price::from(price),
            Price::from(trigger),
            Some(TriggerType::LastPrice),
            Some(TimeInForce::Gtc),
            None,
            Some(false),
            Some(reduce_only),
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            Some(ClientOrderId::new(id)),
        );
        self.submit_order(order, None, None, None)
    }

    fn oco_bracket(&mut self) -> Result<()> {
        let orders = self
            .order()
            .bracket()
            .instrument_id(self.instrument_id)
            .order_side(OrderSide::Buy)
            .quantity(Quantity::from("1.000"))
            .contingency_type(ContingencyType::Oco)
            .entry_order_type(OrderType::Limit)
            .entry_price(Price::from("100.00"))
            .entry_client_order_id(ClientOrderId::new("OCO-ENTRY"))
            .time_in_force(TimeInForce::Gtc)
            .tp_price(Price::from("101.00"))
            .tp_post_only(false)
            .tp_client_order_id(ClientOrderId::new("OCO-TP"))
            .sl_trigger_price(Price::from("99.00"))
            .sl_trigger_type(TriggerType::LastPrice)
            .sl_client_order_id(ClientOrderId::new("OCO-SL"))
            .call();
        self.submit_order_list(orders, None, None, None)
    }

    fn filled_of(&self, id: &str) -> f64 {
        self.cache()
            .order(&ClientOrderId::new(id))
            .map_or(0.0, |order| order.filled_qty().as_f64())
    }

    /// Records one adapter action plus the resulting adapter state. The probe
    /// reads the last snapshot after the engine run, so no private engine
    /// state is needed for any adapter claim.
    fn record_adapter(&self, action: &str, detail: Value) {
        if let Ok(mut slot) = ADAPTER_STATE.lock() {
            *slot = Some(self.adapter.snapshot());
        }
        log_event(
            "adapter",
            json!({"action": action, "detail": detail}),
        );
    }
}

nautilus_strategy!(Passive, {
    fn on_position_opened(&mut self, _event: PositionOpened) {
        if self.mode == Mode::StopGap && self.trades < 1 {
            if let Err(error) = self.stop_limit("PROT-STOP", OrderSide::Sell, "1.000", "99.00", "99.00", true) {
                log_event("submit_error", json!(format!("{error:#}")));
            }
        }
    }

    fn on_order_filled(&mut self, event: &OrderFilled) {
        if let Ok(mut last) = LAST_FILL.lock() {
            *last = Some(event.clone());
        }
        log_event(
            "fill",
            json!({
                "client_order_id": format!("{}", event.client_order_id),
                "qty": event.last_qty.as_f64(),
                "px": format!("{}", event.last_px),
                "ts_event": event.ts_event.as_u64(),
            }),
        );
        if !uses_adapter(self.mode) {
            return;
        }
        let client_order_id = event.client_order_id.to_string();
        if self.adapter.order(&client_order_id).is_none() {
            return;
        }
        // The pinned engine carries no venue sequence on order events, so the
        // adapter stamps one for every event it accepts.
        let venue_seq = self.adapter.stamp_venue_seq();
        let fill_id = event.trade_id.to_string();
        let qty = event.last_qty.as_f64();
        let (verdict, applied) = self
            .adapter
            .note_fill(&client_order_id, venue_seq, &fill_id, qty);
        self.last_fill_id = Some(fill_id.clone());
        self.record_adapter(
            "fill",
            json!({
                "client_order_id": client_order_id,
                "venue_seq": venue_seq,
                "fill_id": fill_id,
                "qty": qty,
                "verdict": verdict.as_str(),
                "applied_qty": applied,
                "ts_event": event.ts_event.as_u64(),
            }),
        );
        if verdict == adapter::EventVerdict::Applied {
            if let Some(group_cancel) = self.adapter.group_sibling_after_fill(&client_order_id) {
                let sibling_id = group_cancel.sibling_id.clone();
                let (decision, _) = self.adapter.request_cancel(&sibling_id);
                self.record_adapter(
                    "group_sibling_cancel",
                    json!({
                        "filled_order_id": group_cancel.filled_order_id,
                        "sibling_id": sibling_id,
                        "policy": "ATOMIC_CLIENT_OCO",
                        "decision": decision.as_str(),
                    }),
                );
                if decision == CancelDecision::DispatchToVenue {
                    if let Err(error) =
                        self.cancel_order(ClientOrderId::new(&sibling_id), None, None)
                    {
                        log_event("cancel_error", json!(format!("{error:#}")));
                    }
                    let response = self.adapter.resolve_cancel(&sibling_id);
                    self.record_adapter(
                        "group_sibling_canceled",
                        json!({"sibling_id": sibling_id, "response": response}),
                    );
                }
            }
        }
    }

    fn on_order_cancel_rejected(&mut self, event: OrderCancelRejected) {
        log_event(
            "cancel_rejected",
            json!({
                "client_order_id": format!("{}", event.client_order_id),
                "reason": event.reason.to_string(),
            }),
        );
    }

    fn on_order_rejected(&mut self, event: OrderRejected) {
        log_event(
            "order_rejected",
            json!({
                "client_order_id": format!("{}", event.client_order_id),
                "reason": event.reason.to_string(),
            }),
        );
    }
});

impl Debug for Passive {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Passive").field("mode", &self.mode).finish()
    }
}

impl DataActor for Passive {
    fn on_start(&mut self) -> anyhow::Result<()> {
        self.subscribe_quotes(self.instrument_id, None, None);
        self.subscribe_trades(self.instrument_id, None, None);
        if self.mode == Mode::F02Depth {
            self.subscribe_book_deltas(self.instrument_id, BookType::L2_MBP, None, None, false, None);
        }
        Ok(())
    }

    fn on_quote(&mut self, quote: &QuoteTick) -> anyhow::Result<()> {
        self.quotes += 1;
        match self.mode {
            Mode::F02 | Mode::F02Cancel => {
                if self.quotes == 1 {
                    self.limit("O-1", OrderSide::Buy, "20.000", "100.00", TimeInForce::Gtc, false, false)?;
                }
                if self.mode == Mode::F02Cancel && self.quotes == 2 {
                    self.cancel_order(ClientOrderId::new("O-1"), None, None)?;
                }
            }
            Mode::TwoLimits => {
                if self.quotes == 1 {
                    self.limit("O-1", OrderSide::Buy, "10.000", "100.00", TimeInForce::Gtc, false, false)?;
                    self.limit("O-2", OrderSide::Buy, "10.000", "100.00", TimeInForce::Gtc, false, false)?;
                }
            }
            Mode::TwoIoc => {
                if self.quotes == 1 {
                    self.market("O-1", OrderSide::Buy, "6.000", TimeInForce::Ioc, false)?;
                    self.market("O-2", OrderSide::Buy, "6.000", TimeInForce::Ioc, false)?;
                }
            }
            Mode::Fok => {
                if self.quotes == 1 {
                    self.limit("O-1", OrderSide::Buy, "6.000", "100.00", TimeInForce::Fok, false, false)?;
                }
            }
            Mode::StopGap => {
                if self.quotes == 1 {
                    self.market("ENTRY", OrderSide::Buy, "1.000", TimeInForce::Ioc, false)?;
                }
            }
            Mode::CancelRace => {
                if self.quotes == 1 {
                    self.limit("O-1", OrderSide::Buy, "1.000", "100.00", TimeInForce::Gtc, false, false)?;
                }
                if self.quotes == 2 {
                    if let Err(error) = self.cancel_order(ClientOrderId::new("O-1"), None, None) {
                        log_event("cancel_error", json!(format!("{error:#}")));
                    }
                }
            }
            Mode::Replace => {
                if self.quotes == 1 {
                    self.limit("O-1", OrderSide::Buy, "10.000", "100.00", TimeInForce::Gtc, false, false)?;
                }
                if self.quotes == 2 {
                    self.modify_order(
                        ClientOrderId::new("O-1"),
                        Some(Quantity::from("20.000")),
                        Some(Price::from("100.00")),
                        None,
                        None,
                        None,
                    )?;
                }
                if self.quotes == 3 {
                    self.modify_order(
                        ClientOrderId::new("O-1"),
                        Some(Quantity::from("15.000")),
                        Some(Price::from("100.00")),
                        None,
                        None,
                        None,
                    )?;
                }
            }
            Mode::PartialBracket => {
                if self.quotes == 1 {
                    self.limit("ENTRY-1", OrderSide::Buy, "10.000", "100.00", TimeInForce::Gtc, false, false)?;
                }
                if self.quotes == 2 && !self.protection_installed {
                    self.protection_installed = true;
                    let filled = self.filled_of("ENTRY-1");
                    // Cancel the unfilled remainder so it is never treated as position.
                    self.cancel_order(ClientOrderId::new("ENTRY-1"), None, None)?;
                    if filled > 0.0 {
                        let tp1 = (filled / 2.0).floor().max(0.0);
                        let tp2 = filled - tp1;
                        let q = |v: f64| format!("{v:.3}");
                        self.stop_limit("PROT-STOP", OrderSide::Sell, &q(filled), "95.00", "95.00", true)?;
                        if tp1 > 0.0 {
                            self.limit("PROT-TP1", OrderSide::Sell, &q(tp1), "105.00", TimeInForce::Gtc, false, true)?;
                        }
                        self.limit("PROT-TP2", OrderSide::Sell, &q(tp2), "110.00", TimeInForce::Gtc, false, true)?;
                        log_event(
                            "protection_installed",
                            json!({"filled_qty": filled, "stop_qty": filled, "tp1_qty": tp1, "tp2_qty": tp2}),
                        );
                    }
                }
            }
            Mode::OcoBracket => {
                if self.quotes == 1 && !self.oco_submitted {
                    self.oco_submitted = true;
                    self.oco_bracket()?;
                }
                if self.quotes == 2 && !self.reduce_only_probe {
                    self.reduce_only_probe = true;
                    self.limit("RO-FLAT", OrderSide::Sell, "1.000", "100.00", TimeInForce::Gtc, false, true)?;
                }
            }
            Mode::F31Dup => {
                if self.quotes == 1 {
                    self.limit("O-1", OrderSide::Buy, "2.000", "100.00", TimeInForce::Gtc, false, false)?;
                }
                if self.quotes == 2 {
                    self.cancel_order(ClientOrderId::new("O-1"), None, None)?;
                }
            }
            Mode::ClientOcoUnsafe => {
                if self.quotes == 1 && !self.oco_submitted {
                    self.oco_submitted = true;
                    // Entry fills immediately; both exits are plain client-managed
                    // orders (no contingency group, no reduce-only flag).
                    self.market("CM-ENTRY", OrderSide::Buy, "1.000", TimeInForce::Ioc, false)?;
                    self.limit("CM-TP", OrderSide::Sell, "1.000", "101.00", TimeInForce::Gtc, false, false)?;
                    self.stop_limit("CM-SL", OrderSide::Sell, "1.000", "98.00", "99.00", false)?;
                }
            }
            Mode::AdapterCancelRace => {
                if self.quotes == 1 {
                    let venue_seq =
                        self.adapter
                            .register("O-1", Side::Buy, 1.0, None, false, 0.0, None);
                    self.record_adapter(
                        "register",
                        json!({
                            "client_order_id": "O-1",
                            "venue_seq": venue_seq,
                            "quantity": 1.0,
                            "ts_event": quote.ts_event.as_u64(),
                        }),
                    );
                    self.limit("O-1", OrderSide::Buy, "1.000", "100.00", TimeInForce::Gtc, false, false)?;
                }
                if self.quotes == 2 {
                    let (decision, response) = self.adapter.request_cancel("O-1");
                    self.record_adapter(
                        "cancel_requested",
                        json!({
                            "client_order_id": "O-1",
                            "decision": decision.as_str(),
                            "immediate_response": response,
                            "delete_latency_ns": 20,
                            "cancel_sent_ns": 10,
                            "cancel_arrival_ns": 30,
                            "ts_event": quote.ts_event.as_u64(),
                        }),
                    );
                    if decision == CancelDecision::DispatchToVenue {
                        self.cancel_order(ClientOrderId::new("O-1"), None, None)?;
                    }
                }
                if self.quotes == 3 {
                    let response = self.adapter.resolve_cancel("O-1");
                    self.record_adapter(
                        "cancel_arrived",
                        json!({
                            "client_order_id": "O-1",
                            "response": response,
                            "ts_event": quote.ts_event.as_u64(),
                        }),
                    );
                }
            }
            Mode::AdapterReplace => {
                let displayed = quote.bid_size.as_f64();
                if self.quotes == 1 {
                    let venue_seq = self.adapter.register(
                        "O-1",
                        Side::Buy,
                        10.0,
                        None,
                        false,
                        displayed,
                        None,
                    );
                    self.record_adapter(
                        "register",
                        json!({
                            "client_order_id": "O-1",
                            "venue_seq": venue_seq,
                            "quantity": 10.0,
                            "ahead_qty": displayed,
                        }),
                    );
                    self.limit("O-1", OrderSide::Buy, "10.000", "100.00", TimeInForce::Gtc, false, false)?;
                }
                if self.quotes == 2 {
                    // Fixture REPLACE #1: 10 -> 20 at the same price. The client
                    // rule resets priority, so the adapter cancels and re-enters.
                    let outcome = self
                        .adapter
                        .request_replace("O-1", 20.0, displayed, true)
                        .map_err(|code| anyhow::anyhow!("replace rejected: {code}"))?;
                    self.record_adapter(
                        "replace",
                        json!({
                            "request_id": outcome.request_id,
                            "new_order_id": outcome.new_order_id,
                            "accepted_qty": outcome.accepted_qty,
                            "filled_qty": outcome.filled_qty,
                            "leaves_qty": outcome.leaves_qty,
                            "ahead_qty": outcome.ahead_qty,
                            "canceled_remainder_qty": outcome.canceled_remainder_qty,
                            "priority_reset": outcome.priority_reset,
                            "increase": outcome.increase,
                            "displayed_at_level": displayed,
                        }),
                    );
                    if outcome.new_order_id != outcome.request_id {
                        self.cancel_order(ClientOrderId::new(&outcome.request_id), None, None)?;
                        let qty = format!("{:.3}", outcome.leaves_qty);
                        self.limit(
                            &outcome.new_order_id,
                            OrderSide::Buy,
                            &qty,
                            "100.00",
                            TimeInForce::Gtc,
                            false,
                            false,
                        )?;
                        self.replace_id = Some(outcome.new_order_id);
                    }
                }
                if self.quotes == 3 {
                    // Fixture REPLACE #2: 20 -> 15 at the same price. A decrease
                    // keeps priority, so the adapter resizes in place.
                    let current = self.replace_id.clone().unwrap_or_else(|| "O-1".to_string());
                    let outcome = self
                        .adapter
                        .request_replace(&current, 15.0, displayed, true)
                        .map_err(|code| anyhow::anyhow!("replace rejected: {code}"))?;
                    self.record_adapter(
                        "replace",
                        json!({
                            "request_id": outcome.request_id,
                            "new_order_id": outcome.new_order_id,
                            "accepted_qty": outcome.accepted_qty,
                            "filled_qty": outcome.filled_qty,
                            "leaves_qty": outcome.leaves_qty,
                            "ahead_qty": outcome.ahead_qty,
                            "canceled_remainder_qty": outcome.canceled_remainder_qty,
                            "priority_reset": outcome.priority_reset,
                            "increase": outcome.increase,
                            "displayed_at_level": displayed,
                        }),
                    );
                    let qty = format!("{:.3}", outcome.leaves_qty);
                    self.modify_order(
                        ClientOrderId::new(&outcome.new_order_id),
                        Some(Quantity::from(qty)),
                        Some(Price::from("100.00")),
                        None,
                        None,
                        None,
                    )?;
                }
            }
            Mode::AdapterEpoch => {
                if self.quotes == 1 {
                    self.adapter
                        .register("E-1", Side::Buy, 1.0, None, false, 0.0, None);
                    self.market("E-1", OrderSide::Buy, "1.000", TimeInForce::Ioc, false)?;
                }
                if self.quotes == 2 {
                    self.adapter
                        .register("E-2", Side::Sell, 1.0, None, false, 0.0, None);
                    self.market("E-2", OrderSide::Sell, "1.000", TimeInForce::Ioc, false)?;
                }
                if self.quotes == 3 {
                    self.adapter
                        .register("E-3", Side::Buy, 1.0, None, false, 0.0, None);
                    self.market("E-3", OrderSide::Buy, "1.000", TimeInForce::Ioc, false)?;
                }
                if self.quotes == 4 {
                    let epoch = self.adapter.position_epoch;
                    // A stop built for the closed position must never reach the venue.
                    let stale = self.adapter.submit_stop_with_epoch(
                        "STALE-STOP",
                        1,
                        Side::Sell,
                        1.0,
                        Some("GR-OLD"),
                        true,
                    );
                    self.record_adapter(
                        "stale_stop",
                        json!({
                            "client_order_id": "STALE-STOP",
                            "requested_epoch": 1,
                            "current_epoch": epoch,
                            "response": match stale { Ok(_) => "ACCEPTED", Err(code) => code },
                            "venue_submission": stale.is_ok(),
                        }),
                    );
                    let qty = self.adapter.position_qty.abs();
                    match self.adapter.submit_stop_with_epoch(
                        "FRESH-STOP",
                        epoch,
                        Side::Sell,
                        qty,
                        None,
                        true,
                    ) {
                        Ok(venue_seq) => {
                            self.record_adapter(
                                "fresh_stop",
                                json!({
                                    "client_order_id": "FRESH-STOP",
                                    "epoch": epoch,
                                    "quantity": qty,
                                    "venue_seq": venue_seq,
                                    "response": "ACCEPTED",
                                    "venue_submission": true,
                                }),
                            );
                            let formatted = format!("{qty:.3}");
                            self.stop_limit(
                                "FRESH-STOP",
                                OrderSide::Sell,
                                &formatted,
                                "98.00",
                                "98.00",
                                true,
                            )?;
                        }
                        Err(code) => self.record_adapter(
                            "fresh_stop",
                            json!({"response": code, "venue_submission": false}),
                        ),
                    }
                }
                if self.quotes == 5 {
                    // Native control: the same stale-epoch stop sent straight to
                    // the venue has no epoch gate at all.
                    self.stop_limit("STALE-NATIVE", OrderSide::Sell, "1.000", "98.00", "98.00", true)?;
                    self.record_adapter(
                        "native_control",
                        json!({
                            "client_order_id": "STALE-NATIVE",
                            "epoch_semantics": 1,
                            "venue_submission": true,
                            "note": "submitted without the adapter epoch gate",
                        }),
                    );
                }
                if self.quotes == 6 {
                    let _ = self.cancel_order(ClientOrderId::new("FRESH-STOP"), None, None);
                    let _ = self.cancel_order(ClientOrderId::new("STALE-NATIVE"), None, None);
                }
            }
            Mode::AdapterF31 => {
                if self.quotes == 1 {
                    let venue_seq =
                        self.adapter
                            .register("O-1", Side::Buy, 2.0, None, false, 0.0, None);
                    self.record_adapter(
                        "register",
                        json!({
                            "client_order_id": "O-1",
                            "venue_seq": venue_seq,
                            "quantity": 2.0,
                        }),
                    );
                    self.limit("O-1", OrderSide::Buy, "2.000", "100.00", TimeInForce::Gtc, false, false)?;
                }
                if self.quotes == 2 {
                    let fill_id = self.last_fill_id.clone().unwrap_or_default();
                    // Delayed ACK: seq 1 arrives after the seq 2 fill.
                    let ack = self.adapter.note_accepted("O-1", 1);
                    self.record_adapter(
                        "delayed_ack",
                        json!({
                            "client_order_id": "O-1",
                            "venue_seq": 1,
                            "event": "ACCEPTED",
                            "verdict": ack.as_str(),
                        }),
                    );
                    // Duplicate fill: same venue seq and trade id.
                    let (duplicate, applied) =
                        self.adapter.note_fill("O-1", 2, &fill_id, 1.0);
                    self.record_adapter(
                        "duplicate_fill",
                        json!({
                            "client_order_id": "O-1",
                            "venue_seq": 2,
                            "fill_id": fill_id,
                            "qty": 1.0,
                            "verdict": duplicate.as_str(),
                            "applied_qty": applied,
                        }),
                    );
                    let (decision, _) = self.adapter.request_cancel("O-1");
                    self.record_adapter(
                        "cancel_requested",
                        json!({"client_order_id": "O-1", "decision": decision.as_str()}),
                    );
                    if decision == CancelDecision::DispatchToVenue {
                        self.cancel_order(ClientOrderId::new("O-1"), None, None)?;
                        let response = self.adapter.resolve_cancel("O-1");
                        self.record_adapter(
                            "cancel_arrived",
                            json!({"client_order_id": "O-1", "response": response}),
                        );
                    }
                }
            }
            Mode::AdapterClientOco => {
                if self.quotes == 1 {
                    self.adapter
                        .register("GR-ENTRY", Side::Buy, 1.0, None, false, 0.0, None);
                    self.market("GR-ENTRY", OrderSide::Buy, "1.000", TimeInForce::Ioc, false)?;
                }
                if self.quotes == 2 {
                    // Client bracket sized on the filled entry quantity only.
                    let filled = self
                        .adapter
                        .order("GR-ENTRY")
                        .map_or(0.0, |order| order.filled);
                    let exit_qty = self.adapter.size_reduce_only_exit(filled, filled);
                    self.adapter.register(
                        "GR-TP",
                        Side::Sell,
                        exit_qty,
                        Some("GR-1"),
                        true,
                        0.0,
                        None,
                    );
                    self.adapter.register(
                        "GR-SL",
                        Side::Sell,
                        exit_qty,
                        Some("GR-1"),
                        true,
                        0.0,
                        None,
                    );
                    self.record_adapter(
                        "bracket_registered",
                        json!({
                            "group": "GR-1",
                            "entry_filled_qty": filled,
                            "tp_qty": exit_qty,
                            "sl_qty": exit_qty,
                            "policy": "SIZED_FROM_FILLED_ENTRY",
                        }),
                    );
                    let formatted = format!("{exit_qty:.3}");
                    self.limit("GR-TP", OrderSide::Sell, &formatted, "101.00", TimeInForce::Gtc, false, true)?;
                    self.stop_limit("GR-SL", OrderSide::Sell, &formatted, "98.00", "98.00", true)?;
                }
                if self.quotes == 3 {
                    let remaining = self
                        .adapter
                        .order("GR-TP")
                        .map_or(0.0, |order| order.quantity);
                    let guard = self.adapter.guard_reduce_only_exit(remaining);
                    self.record_adapter(
                        "reduce_only_guard",
                        json!({
                            "requested_qty": remaining,
                            "position_qty": self.adapter.position_qty,
                            "response": match guard { Ok(qty) => json!(qty), Err(code) => json!(code) },
                            "venue_submission": guard.is_ok(),
                        }),
                    );
                    // Native control: the same exit order sent straight to the
                    // venue, rested away from the market so it cannot fill.
                    self.limit("GR-SL2", OrderSide::Sell, "1.000", "105.00", TimeInForce::Gtc, false, true)?;
                }
                if self.quotes == 4 {
                    let _ = self.cancel_order(ClientOrderId::new("GR-SL2"), None, None);
                    let _ = self.cancel_order(ClientOrderId::new("GR-SL"), None, None);
                }
            }
            Mode::AdapterUnknownAggressor => {
                if self.quotes == 1 {
                    let displayed = quote.bid_size.as_f64();
                    let venue_seq = self.adapter.register(
                        "O-1",
                        Side::Buy,
                        20.0,
                        None,
                        false,
                        displayed,
                        None,
                    );
                    self.record_adapter(
                        "register",
                        json!({
                            "client_order_id": "O-1",
                            "venue_seq": venue_seq,
                            "quantity": 20.0,
                            "ahead_qty": displayed,
                        }),
                    );
                    self.limit("O-1", OrderSide::Buy, "20.000", "100.00", TimeInForce::Gtc, false, false)?;
                }
            }
            Mode::F02Depth => {}
        }
        Ok(())
    }

    fn on_trade(&mut self, tick: &TradeTick) -> anyhow::Result<()> {
        self.trades += 1;
        if self.mode == Mode::AdapterUnknownAggressor {
            let view = aggressor_view(tick.aggressor_side);
            let outcome = self
                .adapter
                .note_trade("O-1", tick.size.as_f64(), view);
            self.record_adapter(
                "trade",
                json!({
                    "aggressor": view.as_str(),
                    "qty": tick.size.as_f64(),
                    "policy": outcome.policy,
                    "depleted_qty": outcome.depleted_qty,
                    "fill_qty": outcome.fill_qty,
                    "ahead_after": outcome.ahead_after,
                    "ts_event": tick.ts_event.as_u64(),
                }),
            );
        }
        Ok(())
    }

    fn on_book_deltas(&mut self, _deltas: &OrderBookDeltas) -> anyhow::Result<()> {
        self.books += 1;
        if self.mode == Mode::F02Depth && self.books == 1 {
            self.limit("O-1", OrderSide::Buy, "20.000", "100.00", TimeInForce::Gtc, false, false)?;
        }
        Ok(())
    }

}

// ---------------------------------------------------------------------------
// Observation helpers
// ---------------------------------------------------------------------------

fn order_of(engine: &BacktestEngine, id: &str) -> Option<OrderAny> {
    engine.kernel().cache.borrow().order(&ClientOrderId::new(id)).map(|order| order.cloned())
}

fn order_snapshot(engine: &BacktestEngine, id: &str) -> Value {
    match order_of(engine, id) {
        Some(order) => {
            let qty = order.quantity().as_f64();
            let filled = order.filled_qty().as_f64();
            let leaves = order.leaves_qty().as_f64();
            let status = order.status();
            json!({
                "client_order_id": id,
                "order_type": format!("{:?}", order.order_type()),
                "status": format!("{status:?}"),
                "quantity": qty,
                "filled_qty": filled,
                "leaves_qty": leaves,
                // Nautilus keeps `leaves_qty` at the unfilled quantity after a cancel and
                // exposes no separate canceled_qty getter, so the canceled quantity is
                // quantity - filled for a terminal cancel.
                "canceled_qty": if matches!(status, OrderStatus::Canceled | OrderStatus::Expired) {
                    Some((qty - filled).max(0.0))
                } else {
                    None
                },
                "avg_px": order.avg_px().map(|px| px.to_string()),
            })
        }
        None => json!({"client_order_id": id, "error": "order not found in cache"}),
    }
}

fn num(snapshot: &Value, key: &str) -> Option<f64> {
    snapshot.get(key).and_then(Value::as_f64)
}

fn position_qty(engine: &BacktestEngine, instrument_id: InstrumentId) -> f64 {
    let cache = engine.kernel().cache.borrow();
    cache
        .positions_open(None, Some(&instrument_id), None, None, None)
        .first()
        .map_or(0.0, |position| position.quantity.as_f64())
}

fn position_signed_qty(engine: &BacktestEngine, instrument_id: InstrumentId) -> f64 {
    let cache = engine.kernel().cache.borrow();
    cache
        .positions_open(None, Some(&instrument_id), None, None, None)
        .first()
        .map_or(0.0, |position| position.signed_qty)
}

fn open_position_count(engine: &BacktestEngine, instrument_id: InstrumentId) -> usize {
    let cache = engine.kernel().cache.borrow();
    cache
        .positions_open(None, Some(&instrument_id), None, None, None)
        .len()
}

fn closed_position_count(engine: &BacktestEngine, instrument_id: InstrumentId) -> usize {
    let cache = engine.kernel().cache.borrow();
    cache
        .positions_closed(None, Some(&instrument_id), None, None, None)
        .len()
}


fn close(a: f64, b: f64) -> bool {
    (a - b).abs() < 1e-9
}

fn probe<F>(f: F) -> Value
where
    F: FnOnce() -> Result<Value>,
{
    let started = Instant::now();
    drain_log();
    match f() {
        Ok(mut value) => {
            if let Value::Object(ref mut map) = value {
                map.insert("elapsed_ms".to_string(), json!(started.elapsed().as_millis() as u64));
                map.insert("events".to_string(), json!(logged("fill")));
                map.insert("cancel_rejections".to_string(), json!(logged("cancel_rejected")));
                map.insert("order_rejections".to_string(), json!(logged("order_rejected")));
                map.insert("cancel_errors".to_string(), json!(logged("cancel_error")));
                map.insert("probe_log".to_string(), json!(logged("protection_installed")));
            }
            value
        }
        Err(error) => json!({
            "error": format!("{error:#}"),
            "elapsed_ms": started.elapsed().as_millis() as u64,
        }),
    }
}

// ---------------------------------------------------------------------------
// Probes
// ---------------------------------------------------------------------------

/// F02 exact: ahead 100, qualifying trades 80 then 30 -> exactly 10 filled,
/// leaves 10, ahead inferred 0. Queue state is not publicly readable.
fn probe_f02() -> Result<Value> {
    let instrument = instrument();
    let id = instrument.id();
    let mut engine = new_engine()?;
    engine.add_venue(venue_config(BookType::L1_MBP, true, true))?;
    engine.add_instrument(&instrument)?;
    engine.add_strategy(Passive::new(id, Mode::F02, "F02"))?;

    run_chunk(
        &mut engine,
        vec![
            quote(id, "100.00", "101.00", "100.000", "10.000", 1),
            trade(id, "100.00", "80.000", AggressorSide::Seller, 2, "F02-T1"),
        ],
    )?;
    let after_trade_1 = order_snapshot(&engine, "O-1");

    run_chunk(&mut engine, vec![trade(id, "100.00", "30.000", AggressorSide::Seller, 3, "F02-T2")])?;
    let after_trade_2 = order_snapshot(&engine, "O-1");
    let position = position_qty(&engine, id);
    engine.end();

    let filled_1 = num(&after_trade_1, "filled_qty").unwrap_or(-1.0);
    let filled_2 = num(&after_trade_2, "filled_qty").unwrap_or(-1.0);
    let fill_step_2 = filled_2 - filled_1;
    let leaves = num(&after_trade_2, "leaves_qty").unwrap_or(-1.0);
    let pass = close(filled_1, 0.0) && close(fill_step_2, 10.0) && close(leaves, 10.0);

    Ok(json!({
        "fixture": "F02",
        "scenario": "BASE",
        "kind": "passive_maker_queue",
        "setup": {"ahead": 100.0, "order_qty": 20.0, "order_price": 100.0, "trades": [80.0, 30.0]},
        "venue_config": "queue_position(true).liquidity_consumption(true).trade_execution(true)",
        "book_type": "L1_MBP",
        "after_trade_1": after_trade_1,
        "after_trade_2": after_trade_2,
        "filled_after_trade_1_qty": filled_1,
        "fill_step_2_qty": fill_step_2,
        "leaves_qty": leaves,
        "position_qty": position,
        "ahead_qty_after": if close(fill_step_2, 10.0) { json!(0.0) } else { Value::Null },
        "queue_state_observable": false,
        "queue_state_note": "ahead is inferred from the exact 10.0 fill on the second trade; queue_ahead_total has no public readback",
        "verdict": if pass { "PASS" } else { "FAIL" },
        "pass": pass,
    }))
}

/// F02 plus cancel remainder: after the partial fill the remainder cancels and
/// a later qualifying trade must not fill it.
fn probe_f02_cancel() -> Result<Value> {
    let instrument = instrument();
    let id = instrument.id();
    let mut engine = new_engine()?;
    engine.add_venue(venue_config(BookType::L1_MBP, true, true))?;
    engine.add_instrument(&instrument)?;
    engine.add_strategy(Passive::new(id, Mode::F02Cancel, "F02C"))?;

    run_chunk(
        &mut engine,
        vec![
            quote(id, "100.00", "101.00", "100.000", "10.000", 1),
            trade(id, "100.00", "80.000", AggressorSide::Seller, 2, "F02C-T1"),
        ],
    )?;
    run_chunk(&mut engine, vec![trade(id, "100.00", "30.000", AggressorSide::Seller, 3, "F02C-T2")])?;
    let after_fill = order_snapshot(&engine, "O-1");

    // Cancel remainder on the second quote, then a further 50-lot trade.
    run_chunk(&mut engine, vec![quote(id, "100.00", "101.00", "100.000", "10.000", 4)])?;
    let after_cancel = order_snapshot(&engine, "O-1");
    run_chunk(&mut engine, vec![trade(id, "100.00", "50.000", AggressorSide::Seller, 5, "F02C-T3")])?;
    let after_extra_trade = order_snapshot(&engine, "O-1");
    engine.end();

    let filled = num(&after_fill, "filled_qty").unwrap_or(-1.0);
    let canceled = num(&after_cancel, "canceled_qty").unwrap_or(-1.0);
    let filled_after = num(&after_extra_trade, "filled_qty").unwrap_or(-1.0);
    let status = after_extra_trade["status"].as_str().unwrap_or("").to_string();
    let pass = close(filled, 10.0) && close(canceled, 10.0) && close(filled_after, 10.0) && status == "Canceled";

    Ok(json!({
        "fixture": "F02+CANCEL_REMAINDER",
        "kind": "partial_fill_then_cancel",
        "after_fill": after_fill,
        "after_cancel": after_cancel,
        "after_extra_trade": after_extra_trade,
        "verdict": if pass { "PASS" } else { "FAIL" },
        "pass": pass,
    }))
}

/// F02 on L2 with a depth reduction after the trade: the depth update and the
/// trade must not double-consume the level.
fn probe_f02_depth_once() -> Result<Value> {
    let instrument = instrument();
    let id = instrument.id();
    let mut engine = new_engine()?;
    engine.add_venue(venue_config(BookType::L2_MBP, true, true))?;
    engine.add_instrument(&instrument)?;
    engine.add_strategy(Passive::new(id, Mode::F02Depth, "F02D"))?;

    run_chunk(
        &mut engine,
        vec![
            delta(id, BookAction::Add, OrderSide::Buy, "100.00", "100.000", 1, 1),
            delta(id, BookAction::Add, OrderSide::Sell, "101.00", "10.000", 2, 1),
            trade(id, "100.00", "80.000", AggressorSide::Seller, 2, "F02D-T1"),
            delta(id, BookAction::Update, OrderSide::Buy, "100.00", "20.000", 3, 3),
        ],
    )?;
    let after_trade_1 = order_snapshot(&engine, "O-1");
    run_chunk(&mut engine, vec![trade(id, "100.00", "30.000", AggressorSide::Seller, 4, "F02D-T2")])?;
    let after_trade_2 = order_snapshot(&engine, "O-1");
    engine.end();

    let filled_1 = num(&after_trade_1, "filled_qty").unwrap_or(-1.0);
    let filled_2 = num(&after_trade_2, "filled_qty").unwrap_or(-1.0);
    let fill_step_2 = filled_2 - filled_1;
    let pass = close(filled_1, 0.0) && close(fill_step_2, 10.0);

    Ok(json!({
        "fixture": "F02+depth-once",
        "kind": "passive_maker_queue_l2",
        "setup": {"ahead": 100.0, "order_qty": 20.0, "trades": [80.0, 30.0], "depth_delta_after_trade": 20.0},
        "book_type": "L2_MBP",
        "after_trade_1": after_trade_1,
        "after_trade_2": after_trade_2,
        "filled_after_trade_1_qty": filled_1,
        "fill_step_2_qty": fill_step_2,
        "depth_reduction_double_consumed": !close(fill_step_2, 10.0),
        "verdict": if pass { "PASS" } else { "FAIL" },
        "pass": pass,
    }))
}

/// Item 4: one shared level budget across two passive orders. If each order
/// gets its own budget the engine over-allocates the level.
fn probe_shared_level() -> Result<Value> {
    let instrument = instrument();
    let id = instrument.id();
    let mut engine = new_engine()?;
    engine.add_venue(venue_config(BookType::L1_MBP, true, true))?;
    engine.add_instrument(&instrument)?;
    engine.add_strategy(Passive::new(id, Mode::TwoLimits, "SHARED"))?;

    run_chunk(
        &mut engine,
        vec![
            quote(id, "100.00", "101.00", "100.000", "10.000", 1),
            trade(id, "100.00", "80.000", AggressorSide::Seller, 2, "SH-T1"),
        ],
    )?;
    let after_trade_1_o1 = order_snapshot(&engine, "O-1");
    let after_trade_1_o2 = order_snapshot(&engine, "O-2");
    run_chunk(&mut engine, vec![trade(id, "100.00", "30.000", AggressorSide::Seller, 3, "SH-T2")])?;
    let o1 = order_snapshot(&engine, "O-1");
    let o2 = order_snapshot(&engine, "O-2");
    engine.end();

    let f1 = num(&o1, "filled_qty").unwrap_or(-1.0);
    let f2 = num(&o2, "filled_qty").unwrap_or(-1.0);
    let total = f1 + f2;
    let level_single_consumed = total <= 10.0 + 1e-9;
    let per_order_budget = total > 10.0 + 1e-9;

    Ok(json!({
        "fixture": "F02+SHARED_LEVEL",
        "kind": "shared_level_budget",
        "setup": {"ahead_per_order": 100.0, "order_qty_each": 10.0, "trades": [80.0, 30.0], "level_qty": 100.0},
        "after_trade_1": {"O-1": after_trade_1_o1, "O-2": after_trade_1_o2},
        "after_trade_2": {"O-1": o1, "O-2": o2},
        "filled_total_qty": total,
        "level_consumed_once": level_single_consumed,
        "per_order_budget_observed": per_order_budget,
        "verdict": if level_single_consumed { "PASS" } else { "PARTIAL" },
        "claim": if level_single_consumed { "shared level budget respected in this setup" } else { "shared level budget NOT asserted; engine allocates per order" },
        "pass": level_single_consumed,
    }))
}

/// F03: two marketable IOC orders against a 10-lot level.
fn probe_f03_shared_liquidity() -> Result<Value> {
    let instrument = instrument();
    let id = instrument.id();
    let mut engine = new_engine()?;
    engine.add_venue(venue_config(BookType::L1_MBP, false, true))?;
    engine.add_instrument(&instrument)?;
    engine.add_strategy(Passive::new(id, Mode::TwoIoc, "F03"))?;

    run_chunk(
        &mut engine,
        vec![quote(id, "99.00", "100.00", "10.000", "10.000", 1)],
    )?;
    let o1 = order_snapshot(&engine, "O-1");
    let o2 = order_snapshot(&engine, "O-2");
    engine.end();

    let f1 = num(&o1, "filled_qty").unwrap_or(-1.0);
    let f2 = num(&o2, "filled_qty").unwrap_or(-1.0);
    let c2 = num(&o2, "canceled_qty").unwrap_or(-1.0);
    let pass = close(f1 + f2, 10.0) && close(f2, 4.0) && close(c2, 2.0);

    Ok(json!({
        "fixture": "F03",
        "scenario": "BASE",
        "kind": "marketable_ioc_shared_level",
        "setup": {"bid": 99.0, "bid_qty": 10.0, "ask": 100.0, "ask_qty": 10.0, "orders": [6.0, 6.0]},
        "O-1": o1,
        "O-2": o2,
        "total_filled_qty": f1 + f2,
        "verdict": if pass { "PASS" } else { "PARTIAL" },
        "note": if pass { "single level consumed once across marketable orders" } else { "marketable-order shared budget not honored; do not claim F03" },
        "pass": pass,
    }))
}

/// F33 FOK: 6 lots against a 5-lot level must cancel in full.
fn probe_f33_fok() -> Result<Value> {
    let instrument = instrument();
    let id = instrument.id();
    let mut engine = new_engine()?;
    engine.add_venue(venue_config(BookType::L1_MBP, false, true))?;
    engine.add_instrument(&instrument)?;
    engine.add_strategy(Passive::new(id, Mode::Fok, "FOK"))?;

    run_chunk(&mut engine, vec![quote(id, "99.00", "100.00", "10.000", "5.000", 1)])?;
    let o1 = order_snapshot(&engine, "O-1");
    engine.end();

    let filled = num(&o1, "filled_qty").unwrap_or(-1.0);
    let canceled = num(&o1, "canceled_qty").unwrap_or(-1.0);
    let pass = close(filled, 0.0) && close(canceled, 6.0);

    Ok(json!({
        "fixture": "F33",
        "scenario": "FOK",
        "kind": "time_in_force",
        "time_in_force_supported": true,
        "O-1": o1,
        "rejections": logged("order_rejected"),
        "verdict": if pass { "PASS" } else { "PARTIAL" },
        "pass": pass,
    }))
}

/// F33 STOP_LIMIT_GAP: a gapped stop-limit triggers but does not fill.
fn probe_f33_stop_gap() -> Result<Value> {
    let instrument = instrument();
    let id = instrument.id();
    let mut engine = new_engine()?;
    engine.add_venue(venue_config(BookType::L1_MBP, false, false))?;
    engine.add_instrument(&instrument)?;
    engine.add_strategy(Passive::new(id, Mode::StopGap, "STOP"))?;

    run_chunk(
        &mut engine,
        vec![
            quote(id, "99.90", "100.00", "10.000", "10.000", 1),
        ],
    )?;
    let position_after_entry = position_qty(&engine, id);
    let stop_after_entry = order_snapshot(&engine, "PROT-STOP");
    run_chunk(
        &mut engine,
        vec![
            quote(id, "98.00", "98.50", "10.000", "10.000", 2),
            trade(id, "98.00", "1.000", AggressorSide::Seller, 3, "F33-T1"),
        ],
    )?;
    let stop = order_snapshot(&engine, "PROT-STOP");
    let position = position_qty(&engine, id);
    engine.end();

    let triggered = stop["status"].as_str().is_some_and(|s| s == "Triggered");
    let filled = num(&stop, "filled_qty").unwrap_or(-1.0);
    let pass = close(position_after_entry, 1.0) && triggered && close(filled, 0.0) && close(position, 1.0);

    Ok(json!({
        "fixture": "F33",
        "scenario": "STOP_LIMIT_GAP",
        "kind": "stop_limit",
        "position_after_entry_qty": position_after_entry,
        "stop_after_entry": stop_after_entry,
        "stop_after_gap": stop,
        "position_after_gap_qty": position,
        "triggered": triggered,
        "filled_qty": filled,
        "verdict": if pass { "PASS" } else { "PARTIAL" },
        "note": "trigger fires on last price; the gapped limit cannot fill and the position is unchanged",
        "pass": pass,
    }))
}

/// F04: a cancel is sent at t=10 with a 20 ns delete latency so it arrives at
/// t=30; a qualifying trade fills the order at t=20. The fill must stand, no
/// overfill may occur, and the late cancel must not reject or double-apply.
/// The fixture's `ALREADY_FILLED` cancel response is not emitted by the engine.
fn probe_f04_cancel_race() -> Result<Value> {
    let instrument = instrument();
    let id = instrument.id();
    let mut engine = new_engine()?;
    // ahead 0 in the fixture: run without the queue model so the resting order
    // is first in line. Delete latency 20 ns defers the cancel from t=10 to t=30.
    engine.add_venue(venue_config_latency(BookType::L1_MBP, false, true, 20))?;
    engine.add_instrument(&instrument)?;
    engine.add_strategy(Passive::new(id, Mode::CancelRace, "F04"))?;

    // t=1: submit (insert latency 0).
    run_chunk(&mut engine, vec![quote(id, "100.00", "100.10", "10.000", "10.000", 1)])?;
    let after_submit = order_snapshot(&engine, "O-1");
    // t=10: cancel sent; arrival is t=30.
    run_chunk(&mut engine, vec![quote(id, "100.00", "100.10", "10.000", "10.000", 10)])?;
    let after_cancel_sent = order_snapshot(&engine, "O-1");
    // t=20: trade fills the resting order before the cancel arrives.
    run_chunk(&mut engine, vec![trade(id, "100.00", "1.000", AggressorSide::Seller, 20, "F04-T1")])?;
    let after_trade = order_snapshot(&engine, "O-1");
    // t=40: next data point forces the t=30 inflight cancel to settle.
    run_chunk(&mut engine, vec![quote(id, "100.00", "100.10", "10.000", "10.000", 40)])?;
    let after_cancel_arrival = order_snapshot(&engine, "O-1");
    engine.end();

    let rejects = logged("cancel_rejected");
    let cancel_errors = logged("cancel_error");
    let filled = num(&after_trade, "filled_qty").unwrap_or(-1.0);
    let final_filled = num(&after_cancel_arrival, "filled_qty").unwrap_or(-1.0);
    let quantity = num(&after_cancel_arrival, "quantity").unwrap_or(0.0);
    let state = after_cancel_arrival["status"].as_str().unwrap_or("").to_string();
    let canceled = if state == "Canceled" {
        (quantity - final_filled).max(0.0)
    } else {
        0.0
    };
    // No overfill: filled + canceled must never exceed the order quantity, and a
    // terminal fill must not gain a canceled quantity.
    let overfill = final_filled + canceled > quantity + 1e-9;
    let pass = close(filled, 1.0) && close(final_filled, 1.0) && state == "Filled" && !overfill;

    Ok(json!({
        "fixture": "F04",
        "scenario": "BASE",
        "kind": "cancel_race",
        "setup": {"order_qty": 1.0, "ahead": 0.0, "cancel_sent_ns": 10, "cancel_arrival_ns": 30, "trade_ns": 20, "delete_latency_ns": 20},
        "after_submit": after_submit,
        "after_cancel_sent": after_cancel_sent,
        "after_trade": after_trade,
        "after_cancel_arrival": after_cancel_arrival,
        "final_filled_qty": final_filled,
        "final_canceled_qty": canceled,
        "overfill": overfill,
        "cancel_response_api_present": false,
        "last_cancel_response": rejects.last().cloned(),
        "cancel_rejection_count": rejects.len(),
        "cancel_errors": cancel_errors,
        "delayed_cancel_arrival_reproduced": true,
        "delayed_cancel_api": "SimulatedVenueConfig::builder().latency_model(Box::new(StaticLatencyModel::new(0, 0, 0, delete_latency_ns)))",
        "missing_api": "OrderCancelRejected / ALREADY_FILLED cancel response: OrderMatchingEngine::process_cancel purges a terminal order silently (crates/execution/src/matching_engine/engine.rs)",
        "verdict": if pass { "PARTIAL" } else { "BLOCKED_BACKEND" },
        "verdict_reason": if pass {
            "deterministic delayed cancel arrival built (CANCEL_SENT at 10, arrival 30); fill stands at t=20, no overfill; the engine emits no ALREADY_FILLED cancel response"
        } else {
            "fill ordering or overfill invariant not satisfied under delayed cancel arrival"
        },
        "pass": pass,
    }))
}

/// F05 native control: replace increase then decrease on the pinned engine.
/// Priority reset count and ahead are not readable, so only the quantity
/// accounting is certified here; the adapter probe reuses this as a control.
fn f05_native_impl() -> Result<Value> {
    let instrument = instrument();
    let id = instrument.id();
    let mut engine = new_engine()?;
    engine.add_venue(venue_config(BookType::L1_MBP, true, true))?;
    engine.add_instrument(&instrument)?;
    engine.add_strategy(Passive::new(id, Mode::Replace, "F05"))?;

    run_chunk(
        &mut engine,
        vec![
            quote(id, "100.00", "101.00", "100.000", "10.000", 1),
            trade(id, "100.00", "102.000", AggressorSide::Seller, 2, "F05-T1"),
        ],
    )?;
    let after_trade = order_snapshot(&engine, "O-1");
    run_chunk(&mut engine, vec![quote(id, "100.00", "101.00", "100.000", "10.000", 3)])?;
    let after_increase = order_snapshot(&engine, "O-1");
    run_chunk(&mut engine, vec![quote(id, "100.00", "101.00", "100.000", "10.000", 4)])?;
    let after_decrease = order_snapshot(&engine, "O-1");
    run_chunk(&mut engine, vec![trade(id, "100.00", "5.000", AggressorSide::Seller, 5, "F05-T2")])?;
    let after_probe_trade = order_snapshot(&engine, "O-1");
    engine.end();

    let filled = num(&after_trade, "filled_qty").unwrap_or(-1.0);
    let leaves = num(&after_decrease, "leaves_qty").unwrap_or(-1.0);
    let accepted = num(&after_decrease, "quantity").unwrap_or(-1.0);
    let probe_fill = num(&after_probe_trade, "filled_qty").unwrap_or(-1.0) - filled;
    let qty_pass = close(filled, 2.0) && close(leaves, 13.0) && close(accepted, 15.0);

    Ok(json!({
        "fixture": "F05",
        "scenario": "BASE",
        "kind": "replace_priority",
        "after_trade": after_trade,
        "after_increase": after_increase,
        "after_decrease": after_decrease,
        "after_probe_trade": after_probe_trade,
        "filled_qty": filled,
        "leaves_qty": leaves,
        "accepted_qty": accepted,
        "priority_probe_fill_after_increase_qty": probe_fill,
        "priority_probe_inference": if close(probe_fill, 0.0) {
            "no fill after replace increase: priority appears reset to the back of the queue"
        } else {
            "fill after replace increase: queue position was preserved, not reset"
        },
        "observed_priority_behavior": if close(probe_fill, 0.0) {
            "reset_on_same_price_increase"
        } else {
            "preserved_on_same_price_increase"
        },
        "fixture_expects_priority_reset_on_increase": true,
        "fixture_priority_reset_count_expected": 1,
        "native_matches_fixture_priority": close(probe_fill, 0.0),
        "priority_reset_count_public": false,
        "ahead_qty_public": false,
        "engine_path": "OrderMatchingEngine::process_modify only calls snapshot_queue_position when the price changed; a same-price quantity change does not reset the tracked queue",
        "missing_api": "no public priority_reset_count and no public queue-ahead readback; OrderMatchingEngine.queue_ahead_total is private",
        "priority_reset_count": null,
        "ahead_qty": null,
        "verdict": if qty_pass { "PARTIAL" } else { "BLOCKED_BACKEND" },
        "verdict_reason": if qty_pass {
            "replace quantity accounting reproduced; priority_reset_count and ahead_qty have no public readback"
        } else {
            "replace quantity accounting not reproduced"
        },
        "pass": qty_pass,
    }))
}

/// F05 native probe. Kept unchanged so the existing verdict and evidence
/// fields are preserved; the adapter probe reuses `f05_native_impl` as a
/// control.
fn probe_f05_replace() -> Result<Value> {
    f05_native_impl()
}

/// F34: partial maker entry then client-managed protection sized on the filled
/// quantity only. Unfilled entry quantity is canceled, not protected.
fn probe_f34_partial_entry() -> Result<Value> {
    let instrument = instrument();
    let id = instrument.id();
    let mut engine = new_engine()?;
    engine.add_venue(venue_config(BookType::L1_MBP, true, true))?;
    engine.add_instrument(&instrument)?;
    engine.add_strategy(Passive::new(id, Mode::PartialBracket, "F34"))?;

    run_chunk(
        &mut engine,
        vec![
            quote(id, "100.00", "101.00", "100.000", "10.000", 1),
            trade(id, "100.00", "103.000", AggressorSide::Seller, 2, "F34-T1"),
        ],
    )?;
    let entry_after_partial = order_snapshot(&engine, "ENTRY-1");
    let position_after_partial = position_qty(&engine, id);
    run_chunk(&mut engine, vec![quote(id, "100.00", "101.00", "100.000", "10.000", 3)])?;
    let entry_after_protection = order_snapshot(&engine, "ENTRY-1");
    let stop = order_snapshot(&engine, "PROT-STOP");
    let tp1 = order_snapshot(&engine, "PROT-TP1");
    let tp2 = order_snapshot(&engine, "PROT-TP2");
    let position = position_qty(&engine, id);
    engine.end();

    let filled = num(&entry_after_partial, "filled_qty").unwrap_or(-1.0);
    let stop_qty = num(&stop, "quantity").unwrap_or(-1.0);
    let tp1_qty = num(&tp1, "quantity").unwrap_or(-1.0);
    let tp2_qty = num(&tp2, "quantity").unwrap_or(-1.0);
    let canceled = num(&entry_after_protection, "canceled_qty").unwrap_or(-1.0);
    let pass = close(filled, 3.0)
        && close(stop_qty, 3.0)
        && close(tp1_qty, 1.0)
        && close(tp2_qty, 2.0)
        && close(tp1_qty + tp2_qty, 3.0)
        && close(canceled, 7.0)
        && close(position, 3.0);

    Ok(json!({
        "fixture": "F34",
        "scenario": "BASE",
        "kind": "partial_entry_protection",
        "setup": {"entry_qty": 10.0, "filled_qty": 3.0, "lot": 1.0},
        "entry_after_partial": entry_after_partial,
        "entry_after_protection": entry_after_protection,
        "position_after_partial_qty": position_after_partial,
        "stop": stop,
        "tp1": tp1,
        "tp2": tp2,
        "stop_qty": stop_qty,
        "tp1_qty": tp1_qty,
        "tp2_qty": tp2_qty,
        "exit_total_qty": tp1_qty + tp2_qty,
        "canceled_entry_remainder_qty": canceled,
        "unfilled_entry_protected_as_position": !close(canceled, 7.0),
        "position_qty": position,
        "verdict": if pass { "PASS" } else { "FAIL" },
        "note": "protection is client-managed: the native bracket supports one TP plus one SL, so the split TP is built here",
        "pass": pass,
    }))
}

/// F06: native OCO bracket, then a reduce-only order while flat must be rejected.
fn probe_f06_oco() -> Result<Value> {
    let instrument = instrument();
    let id = instrument.id();
    let mut engine = new_engine()?;
    engine.add_venue(venue_config(BookType::L1_MBP, false, true))?;
    engine.add_instrument(&instrument)?;
    engine.add_strategy(Passive::new(id, Mode::OcoBracket, "F06"))?;

    run_chunk(
        &mut engine,
        vec![
            quote(id, "99.90", "100.00", "10.000", "10.000", 1),
            trade(id, "100.00", "1.000", AggressorSide::Seller, 2, "F06-T1"),
        ],
    )?;
    let after_entry = {
        let entry = order_snapshot(&engine, "OCO-ENTRY");
        let tp = order_snapshot(&engine, "OCO-TP");
        let sl = order_snapshot(&engine, "OCO-SL");
        json!({"entry": entry, "tp": tp, "sl": sl, "position_qty": position_qty(&engine, id)})
    };
    run_chunk(&mut engine, vec![quote(id, "101.50", "101.60", "10.000", "10.000", 3)])?;
    let after_tp = {
        let tp = order_snapshot(&engine, "OCO-TP");
        let sl = order_snapshot(&engine, "OCO-SL");
        let ro = order_snapshot(&engine, "RO-FLAT");
        json!({
            "tp": tp,
            "sl": sl,
            "reduce_only_flat": ro,
            "position_qty": position_qty(&engine, id),
            "open_positions": open_position_count(&engine, id),
            "closed_positions": closed_position_count(&engine, id),
        })
    };
    engine.end();

    let position = num(&after_tp, "position_qty").unwrap_or(-1.0);
    let tp_status = after_tp["tp"]["status"].as_str().unwrap_or("");
    let sl_status = after_tp["sl"]["status"].as_str().unwrap_or("");
    let ro_status = after_tp["reduce_only_flat"]["status"].as_str().unwrap_or("");
    let rejects = logged("order_rejected");
    let pass = close(position, 0.0) && tp_status == "Filled" && sl_status == "Canceled" && ro_status != "Filled";

    Ok(json!({
        "fixture": "F06",
        "scenario": "NATIVE_ATOMIC",
        "kind": "native_oco",
        "oco_supported": true,
        "after_entry": after_entry,
        "after_tp": after_tp,
        "reduce_only_rejections": rejects,
        "verdict": if pass { "PASS" } else { "PARTIAL" },
        "client_managed_profile": {
            "verdict": "BLOCKED_BACKEND",
            "reason": "no API to force both OCO legs eligible at one price; a single price path cannot fill both a 99 stop and a 101 limit, so the CLIENT_UNSAFE_OBSERVABLE overfill is not reproducible natively"
        },
        "pass": pass,
    }))
}

// ---------------------------------------------------------------------------
// Static capability record for fixtures with no reachable hook
// ---------------------------------------------------------------------------

/// F29: sequence-gap and resync behavior of the public `OrderBook`.
///
/// The book exposes `sequence` but has no anchor, gap validation, `last_good_seq`
/// or INVALID state. This probe feeds a forward gap and an out-of-order delta to
/// the exact public book class the engine uses and records what it does.
fn probe_f29_book_sequence() -> Result<Value> {
    let instrument = instrument();
    let id = instrument.id();
    let mut book = OrderBook::new(id, BookType::L2_MBP);

    let add_bid = OrderBookDelta::new(
        id,
        BookAction::Add,
        BookOrder::new(OrderSide::Buy, Price::from("100.00"), Quantity::from("100.000"), 1),
        0,
        100,
        ts(1),
        ts(1),
    );
    let add_ask = OrderBookDelta::new(
        id,
        BookAction::Add,
        BookOrder::new(OrderSide::Sell, Price::from("101.00"), Quantity::from("10.000"), 2),
        0,
        101,
        ts(2),
        ts(2),
    );
    let anchored = OrderBookDelta::new(
        id,
        BookAction::Update,
        BookOrder::new(OrderSide::Buy, Price::from("100.00"), Quantity::from("100.000"), 1),
        0,
        102,
        ts(3),
        ts(3),
    );
    // Forward gap: expected next sequence is 103, this jumps to 110.
    let gapped = OrderBookDelta::new(
        id,
        BookAction::Add,
        BookOrder::new(OrderSide::Buy, Price::from("99.00"), Quantity::from("5.000"), 3),
        0,
        110,
        ts(4),
        ts(4),
    );
    // Out of order: sequence 105 after 110; also an absolute size update to 20.
    let out_of_order = OrderBookDelta::new(
        id,
        BookAction::Update,
        BookOrder::new(OrderSide::Buy, Price::from("100.00"), Quantity::from("20.000"), 1),
        0,
        105,
        ts(5),
        ts(5),
    );

    let mut errors: Vec<Option<String>> = Vec::new();
    for delta in [&add_bid, &add_ask, &anchored, &gapped, &out_of_order] {
        errors.push(book.apply_delta(delta).err().map(|e| e.to_string()));
    }
    let gap_detected = errors[3].is_some();
    let out_of_order_detected = errors[4].is_some();
    let level_qty = book
        .get_quantity_at_level(Price::from("100.00"), OrderSide::Sell, 2)
        .as_f64();
    let sequence_high_water = book.sequence;
    let best_bid = book.best_bid_price().map(|px| px.to_string());
    let absolute_updates_not_additive = close(level_qty, 20.0);

    Ok(json!({
        "fixture": "F29",
        "scenario": "BASE",
        "kind": "book_sequence",
        "book_type": "L2_MBP",
        "sequence_stream": [100, 101, 102, 110, 105],
        "apply_errors": errors,
        "gap_expected_between": [103, 109],
        "gap_detected": gap_detected,
        "out_of_order_detected": out_of_order_detected,
        "sequence_high_water": sequence_high_water,
        "sequence_regressed": sequence_high_water < 110,
        "best_bid": best_bid,
        "level_qty_after_absolute_update": level_qty,
        "absolute_updates_not_additive": absolute_updates_not_additive,
        "book_state_api_present": false,
        "last_good_seq_api_present": false,
        "snapshot_anchor_api_present": false,
        "missing_api": "OrderBook.sequence is public but there is no last_good_seq, book_state (INVALID) or sequence-gap API; apply_delta/apply_deltas never validate sequence continuity and BookIntegrityError has no gap variant",
        "verdict": "BLOCKED_BACKEND",
        "verdict_reason": "the fixture needs book_state=INVALID and last_good_seq=102; the public book silently applies both the forward gap and the out-of-order delta (high-water mark only)",
    }))
}

/// F31: inject a duplicate fill into the public OMS entry point and check that
/// it is suppressed. There is no venue-sequence field, so the delayed/out-of-order
/// ACK half of the fixture is not reproducible.
fn probe_f31_duplicate_fill() -> Result<Value> {
    let instrument = instrument();
    let id = instrument.id();
    let mut engine = new_engine()?;
    engine.add_venue(venue_config(BookType::L1_MBP, false, true))?;
    engine.add_instrument(&instrument)?;
    engine.add_strategy(Passive::new(id, Mode::F31Dup, "F31"))?;

    // t=1 submit buy 2 @ 100; t=2 trade 1 -> partial fill of 1.
    run_chunk(
        &mut engine,
        vec![
            quote(id, "100.00", "100.10", "10.000", "10.000", 1),
            trade(id, "100.00", "1.000", AggressorSide::Seller, 2, "F31-FILL-1"),
        ],
    )?;
    let after_partial = order_snapshot(&engine, "O-1");
    let fill = LAST_FILL.lock().ok().and_then(|last| last.clone());
    let filled_events_before = logged("fill").len();
    let duplicate_predicate = match (&fill, order_of(&engine, "O-1")) {
        (Some(fill), Some(order)) => order.is_duplicate_fill(fill),
        _ => false,
    };

    // Public OMS event injection: ExecutionEngine::process(&OrderEventAny).
    if let Some(fill) = fill.clone() {
        engine
            .kernel_mut()
            .exec_engine
            .borrow_mut()
            .process(&OrderEventAny::Filled(fill));
    }
    let after_duplicate = order_snapshot(&engine, "O-1");
    let filled_events_after = logged("fill").len();

    // t=4: cancel the remainder.
    run_chunk(&mut engine, vec![quote(id, "100.00", "100.10", "10.000", "10.000", 4)])?;
    let after_cancel = order_snapshot(&engine, "O-1");
    engine.end();

    let accepted = num(&after_cancel, "quantity").unwrap_or(-1.0);
    let filled = num(&after_cancel, "filled_qty").unwrap_or(-1.0);
    let leaves = num(&after_cancel, "leaves_qty").unwrap_or(-1.0);
    let canceled = num(&after_cancel, "canceled_qty").unwrap_or(-1.0);
    let state = after_cancel["status"].as_str().unwrap_or("").to_string();
    let duplicate_suppressed = close(filled, 1.0)
        && close(num(&after_duplicate, "filled_qty").unwrap_or(-1.0), 1.0)
        && filled_events_after == filled_events_before;
    let core_pass = close(accepted, 2.0)
        && close(filled, 1.0)
        && close(canceled, 1.0)
        && state == "Canceled"
        && duplicate_suppressed;

    Ok(json!({
        "fixture": "F31",
        "scenario": "BASE",
        "kind": "duplicate_fill",
        "after_partial": after_partial,
        "after_duplicate_injection": after_duplicate,
        "after_cancel": after_cancel,
        "accepted_qty": accepted,
        "filled_qty": filled,
        "leaves_qty": leaves,
        "canceled_qty": canceled,
        "state": state,
        "duplicate_fill_predicate": duplicate_predicate,
        "duplicate_suppressed": duplicate_suppressed,
        "strategy_fill_events_before_injection": filled_events_before,
        "strategy_fill_events_after_injection": filled_events_after,
        "duplicate_fee_or_funding_debits": 0,
        "injection_api": "BacktestEngine::kernel_mut().exec_engine.borrow_mut().process(&OrderEventAny::Filled(fill))",
        "event_order_api_present": false,
        "venue_seq_field_present": false,
        "local_order_event_data_hook_present": false,
        "fixture_expects_leaves_qty": 0.0,
        "native_leaves_qty_after_cancel": leaves,
        "missing_api": "OrderFilled/OrderAccepted carry no venue_seq; BacktestEngine/Data has no LOCAL_ORDER_EVENT injection operation, so the out-of-order ACK (seq 2 then seq 1) cannot be replayed. Duplicate-fill suppression itself is reachable through ExecutionEngine::process",
        "verdict": if core_pass { "PARTIAL" } else { "BLOCKED_BACKEND" },
        "verdict_reason": if core_pass {
            "duplicate fill suppressed by the public OMS (filled=1, canceled=1, Canceled, no second strategy fill); delayed/out-of-order ACK and the fixture's leaves_qty=0 are not reproducible"
        } else {
            "duplicate fill suppression or cancel accounting not reproduced"
        },
        "pass": core_pass,
    }))
}

/// Client-managed OCO: both exits are plain orders, so a stop exit followed by a
/// take-profit exit can both fill and reverse the position. This is the unsafe
/// observable; it is deliberately separate from the native OCO result in F06.
fn probe_client_oco_overfill() -> Result<Value> {
    let instrument = instrument();
    let id = instrument.id();
    let mut engine = new_engine()?;
    engine.add_venue(venue_config(BookType::L1_MBP, false, false))?;
    engine.add_instrument(&instrument)?;
    engine.add_strategy(Passive::new(id, Mode::ClientOcoUnsafe, "CMOCO"))?;

    // t=1: buy 1 at 100, then rest a plain TP sell 1 @ 101 and a plain SL sell 1 @ 98.
    run_chunk(&mut engine, vec![quote(id, "99.90", "100.00", "10.000", "10.000", 1)])?;
    let after_entry = {
        json!({
            "entry": order_snapshot(&engine, "CM-ENTRY"),
            "tp": order_snapshot(&engine, "CM-TP"),
            "sl": order_snapshot(&engine, "CM-SL"),
            "position_qty": position_qty(&engine, id),
        })
    };

    // t=2/t=3: price drops through the stop; the stop sells 1 and flattens.
    run_chunk(
        &mut engine,
        vec![
            quote(id, "98.00", "98.50", "10.000", "10.000", 2),
            trade(id, "98.00", "1.000", AggressorSide::Seller, 3, "CM-T1"),
        ],
    )?;
    let after_stop = {
        json!({
            "tp": order_snapshot(&engine, "CM-TP"),
            "sl": order_snapshot(&engine, "CM-SL"),
            "position_qty": position_qty(&engine, id),
            "position_signed_qty": position_signed_qty(&engine, id),
            "open_positions": open_position_count(&engine, id),
            "closed_positions": closed_position_count(&engine, id),
        })
    };

    // t=4/t=5: price rises through the take profit; the still-open TP sells 1
    // again and opens a short because it was never linked to the first exit.
    run_chunk(
        &mut engine,
        vec![
            quote(id, "102.00", "102.50", "10.000", "10.000", 4),
            trade(id, "102.00", "1.000", AggressorSide::Buyer, 5, "CM-T2"),
        ],
    )?;
    let after_tp = {
        json!({
            "tp": order_snapshot(&engine, "CM-TP"),
            "sl": order_snapshot(&engine, "CM-SL"),
            "position_qty": position_qty(&engine, id),
            "position_signed_qty": position_signed_qty(&engine, id),
            "position_side": engine
                .kernel()
                .cache
                .borrow()
                .positions_open(None, Some(&id), None, None, None)
                .first()
                .map(|p| format!("{:?}", p.side))
                .unwrap_or_else(|| "Flat".to_string()),
            "open_positions": open_position_count(&engine, id),
            "closed_positions": closed_position_count(&engine, id),
        })
    };
    engine.end();

    let entry_filled = num(&after_entry["entry"], "filled_qty").unwrap_or(-1.0);
    let stop_filled = num(&after_stop["sl"], "filled_qty").unwrap_or(-1.0);
    let tp_filled = num(&after_tp["tp"], "filled_qty").unwrap_or(-1.0);
    let exit_total = stop_filled + tp_filled;
    let position_after = num(&after_tp, "position_signed_qty").unwrap_or(0.0);
    let overfill = exit_total > entry_filled + 1e-9;

    Ok(json!({
        "fixture": "F06_CLIENT_UNSAFE_OBSERVABLE",
        "scenario": "CLIENT_MANAGED",
        "kind": "client_managed_oco_overfill",
        "after_entry": after_entry,
        "after_stop_fill": after_stop,
        "after_tp_fill": after_tp,
        "entry_filled_qty": entry_filled,
        "stop_filled_qty": stop_filled,
        "tp_filled_qty": tp_filled,
        "exit_total_qty": exit_total,
        "entry_qty": 1.0,
        "overfill_observed": overfill,
        "over_exit_qty": exit_total - entry_filled,
        "position_qty_after": position_after,
        "position_side_after": after_tp["position_side"],
        "position_reversed": position_after < 0.0,
        "native_oco_sibling_cancelled": false,
        "control_reference": "F06 NATIVE_ATOMIC: the OCO sibling is canceled when one leg fills",
        "missing_api": "no client-order group/epoch advisory or synthetic both-legs-eligible event; the overfill is driven by an ordinary two-phase price path instead",
        "verdict": if overfill { "PARTIAL" } else { "BLOCKED_BACKEND" },
        "verdict_reason": if overfill {
            "unsafe observable reproduced: 2 exits for a 1-lot entry, position reversed to -1; native OCO prevents this only when the bracket is submitted as an OCO list"
        } else {
            "client-managed overfill was not reproduced"
        },
        "pass": overfill,
    }))
}

// ---------------------------------------------------------------------------
// Adapter-over-native probes (profile ADAPTER_OVER_NATIVE)
//
// Each probe drives the pinned native engine as the venue and keeps the
// client-owned facts in `adapter::Adapter`. Every probe names the exact native
// limitation next to the behavior it compensates for.
// ---------------------------------------------------------------------------

/// F04 over the adapter: the cancel is dispatched at t=10 and arrives at t=30
/// through the native delete latency, after the order filled at t=20. The
/// adapter answers the arrival with an explicit `ALREADY_FILLED`.
fn probe_f04_adapter() -> Result<Value> {
    take_adapter_state();
    let instrument = instrument();
    let id = instrument.id();
    let mut engine = new_engine()?;
    engine.add_venue(venue_config_latency(BookType::L1_MBP, false, true, 20))?;
    engine.add_instrument(&instrument)?;
    engine.add_strategy(Passive::new(id, Mode::AdapterCancelRace, "F04A"))?;

    run_chunk(
        &mut engine,
        vec![quote(id, "100.00", "100.10", "10.000", "10.000", 1)],
    )?;
    let native_after_submit = order_snapshot(&engine, "O-1");
    run_chunk(
        &mut engine,
        vec![quote(id, "100.00", "100.10", "10.000", "10.000", 10)],
    )?;
    let native_after_cancel_sent = order_snapshot(&engine, "O-1");
    run_chunk(
        &mut engine,
        vec![trade(id, "100.00", "1.000", AggressorSide::Seller, 20, "F04A-T1")],
    )?;
    let native_after_trade = order_snapshot(&engine, "O-1");
    run_chunk(
        &mut engine,
        vec![quote(id, "100.00", "100.10", "10.000", "10.000", 40)],
    )?;
    let native_after_arrival = order_snapshot(&engine, "O-1");
    engine.end();

    let adapter = take_adapter_state();
    let local = adapter_order(&adapter, "O-1");
    let local_filled = num(&local, "filled_qty").unwrap_or(-1.0);
    let local_canceled = num(&local, "canceled_qty").unwrap_or(0.0);
    let local_quantity = num(&local, "quantity").unwrap_or(0.0);
    let local_state = local["state"].as_str().unwrap_or("").to_string();
    let last_response = adapter["last_cancel_response"].as_str().unwrap_or("");
    let overfill = local_filled + local_canceled > local_quantity + 1e-9;
    let cancel_requested = adapter_detail("cancel_requested");
    let cancel_arrived = adapter_detail("cancel_arrived");
    let delayed_arrival = cancel_requested["decision"] == "DISPATCH_TO_VENUE"
        && num(&native_after_trade, "filled_qty") == Some(1.0)
        && native_after_arrival["status"] == "Filled"
        && cancel_arrived["ts_event"].as_u64() == Some(1_000_000_040);
    let pass = close(local_filled, 1.0)
        && local_state == "FILLED"
        && last_response == "ALREADY_FILLED"
        && !overfill
        && delayed_arrival;

    Ok(json!({
        "fixture": "F04",
        "scenario": "ADAPTER_OVER_NATIVE",
        "kind": "cancel_race",
        "profile": adapter::PROFILE,
        "setup": {
            "order_qty": 1.0,
            "ahead": 0.0,
            "cancel_sent_ns": 10,
            "cancel_arrival_ns": 30,
            "trade_ns": 20,
            "delete_latency_ns": 20,
        },
        "filled_qty": local_filled,
        "state": local_state.to_uppercase(),
        "last_cancel_response": last_response,
        "overfill": overfill,
        "delayed_cancel_arrival_reproduced": delayed_arrival,
        "adapter": {
            "cancel_requested": cancel_requested,
            "cancel_arrived": cancel_arrived,
            "cancel_response_count": adapter["cancel_responses"].as_array().map_or(0, Vec::len),
            "local_order": local,
            "strategy_fill_events": adapter["strategy_fill_events"],
            "fills": adapter_actions("fill"),
        },
        "native": {
            "after_submit": native_after_submit,
            "after_cancel_sent": native_after_cancel_sent,
            "after_trade": native_after_trade,
            "after_cancel_arrival": native_after_arrival,
            "cancel_rejections": logged("cancel_rejected"),
        },
        "native_limitation": "OrderMatchingEngine::process_cancel purges a terminal order silently (crates/execution/src/matching_engine/engine.rs); OrderCancelRejected is never emitted for ALREADY_FILLED",
        "verdict": if pass { "PASS" } else { "FAIL" },
        "verdict_reason": if pass {
            "adapter local state answers the late cancel with ALREADY_FILLED at the arrival boundary; native fill stands at 1 lot and no quantity is overfilled"
        } else {
            "adapter cancel response, fill or overfill invariant not satisfied"
        },
        "pass": pass,
    }))
}

/// F05 over the adapter: the increase is a client-managed cancel+new (priority
/// reset counted by the adapter), the decrease is an in-place resize. The
/// native control records that a same-price native replace keeps priority.
fn probe_f05_adapter() -> Result<Value> {
    // Native control first; its fill log is drained before the adapter run.
    let native_control_full = f05_native_impl()?;
    drain_log();
    take_adapter_state();

    let native_control = json!({
        "observed_priority_behavior": native_control_full["observed_priority_behavior"],
        "priority_probe_fill_after_increase_qty": native_control_full["priority_probe_fill_after_increase_qty"],
        "native_matches_fixture_priority": native_control_full["native_matches_fixture_priority"],
        "priority_reset_count_public": false,
        "ahead_qty_public": false,
        "missing_api": native_control_full["missing_api"],
        "engine_path": native_control_full["engine_path"],
    });

    let instrument = instrument();
    let id = instrument.id();
    let mut engine = new_engine()?;
    engine.add_venue(venue_config(BookType::L1_MBP, true, true))?;
    engine.add_instrument(&instrument)?;
    engine.add_strategy(Passive::new(id, Mode::AdapterReplace, "F05A"))?;

    // t=1 submit 10 @ 100 (displayed 100); t=2 a 102-lot trade fills 2.
    run_chunk(
        &mut engine,
        vec![
            quote(id, "100.00", "101.00", "100.000", "10.000", 1),
            trade(id, "100.00", "102.000", AggressorSide::Seller, 2, "F05A-T1"),
        ],
    )?;
    let native_after_trade = order_snapshot(&engine, "O-1");
    // t=3 replace increase 10 -> 20 (client cancel+new, priority reset).
    run_chunk(
        &mut engine,
        vec![quote(id, "100.00", "101.00", "100.000", "10.000", 3)],
    )?;
    // t=4 replace decrease 20 -> 15 (in-place resize, no reset).
    run_chunk(
        &mut engine,
        vec![quote(id, "100.00", "101.00", "100.000", "10.000", 4)],
    )?;
    engine.end();

    let adapter = take_adapter_state();
    let replaces = adapter_actions("replace");
    let first = replaces.first().cloned().unwrap_or(Value::Null);
    let second = replaces.get(1).cloned().unwrap_or(Value::Null);
    let successor_id = first["new_order_id"].as_str().unwrap_or("O-1-R1");
    let successor = adapter_order(&adapter, successor_id);
    let filled = num(&successor, "filled_qty").unwrap_or(-1.0);
    let leaves = num(&successor, "leaves_qty").unwrap_or(-1.0);
    let accepted = num(&successor, "quantity").unwrap_or(-1.0);
    let ahead = num(&successor, "ahead_qty").unwrap_or(-1.0);
    let priority_reset_count = adapter["priority_reset_count"].as_u64().unwrap_or(0);
    let native_successor = order_snapshot(&engine, successor_id);
    let quantity_pass = close(filled, 2.0)
        && close(leaves, 13.0)
        && close(accepted, 15.0)
        && close(ahead, 100.0)
        && priority_reset_count == 1;
    let pass = quantity_pass
        && first["priority_reset"] == true
        && second["priority_reset"] == false
        && native_control["observed_priority_behavior"] == "preserved_on_same_price_increase";

    Ok(json!({
        "fixture": "F05",
        "scenario": "ADAPTER_OVER_NATIVE",
        "kind": "replace_priority",
        "profile": adapter::PROFILE,
        "setup": {
            "order_qty": 10.0,
            "ahead": 100.0,
            "replace_increase_resets": true,
            "replace_decrease_resets": false,
            "sequence": [10.0, 20.0, 15.0],
        },
        "filled_qty": filled,
        "leaves_qty": leaves,
        "accepted_qty": accepted,
        "ahead_qty": ahead,
        "priority_reset_count": priority_reset_count,
        "replace_increase_resets": true,
        "adapter": {
            "replaces": replaces,
            "successor": successor,
            "canceled_original": adapter_order(&adapter, "O-1"),
            "replace_count": adapter["replace_count"],
        },
        "native_control": native_control,
        "native_mirror": {
            "after_trade": native_after_trade,
            "successor_after_modify": native_successor,
            "note": "the venue successor carries the remaining quantity (13) while the client aggregate carries 2 filled + 13 leaves = 15 accepted",
        },
        "profile_boundary": {
            "adapter_managed": "cancel+new replace when priority must reset; in-place resize otherwise",
            "native_gap": "no public priority_reset_count and no public queue-ahead readback; OrderMatchingEngine::process_modify only re-snapshots the queue on a price change",
        },
        "verdict": if pass { "PASS" } else { "FAIL" },
        "verdict_reason": if pass {
            "adapter priority_reset_count=1 with cancel+new on the increase; quantities match the oracle; native same-price replace preserves priority and remains recorded"
        } else {
            "adapter replace quantities, priority reset count or native control mismatch"
        },
        "pass": pass,
    }))
}

/// F07 over the adapter: the position epoch is stamped on open, and a stop
/// built for the closed position is rejected before any venue submission.
fn probe_f07_adapter() -> Result<Value> {
    take_adapter_state();
    let instrument = instrument();
    let id = instrument.id();
    let mut engine = new_engine()?;
    engine.add_venue(venue_config(BookType::L1_MBP, false, true))?;
    engine.add_instrument(&instrument)?;
    engine.add_strategy(Passive::new(id, Mode::AdapterEpoch, "F07A"))?;

    // Epoch 1: open. Then close. Then reopen under epoch 2.
    run_chunk(
        &mut engine,
        vec![quote(id, "100.00", "100.10", "10.000", "10.000", 1)],
    )?;
    let epoch_1_fill = order_snapshot(&engine, "E-1");
    run_chunk(
        &mut engine,
        vec![quote(id, "100.00", "100.10", "10.000", "10.000", 2)],
    )?;
    let close_fill = order_snapshot(&engine, "E-2");
    run_chunk(
        &mut engine,
        vec![quote(id, "100.00", "100.10", "10.000", "10.000", 3)],
    )?;
    let reopen_fill = order_snapshot(&engine, "E-3");
    run_chunk(
        &mut engine,
        vec![quote(id, "100.00", "100.10", "10.000", "10.000", 4)],
    )?;
    let fresh_after_submit = order_snapshot(&engine, "FRESH-STOP");
    let stale_native_absent = order_of(&engine, "STALE-STOP").is_none();
    run_chunk(
        &mut engine,
        vec![quote(id, "100.00", "100.10", "10.000", "10.000", 5)],
    )?;
    let native_control_after_submit = order_snapshot(&engine, "STALE-NATIVE");
    run_chunk(
        &mut engine,
        vec![quote(id, "100.00", "100.10", "10.000", "10.000", 6)],
    )?;
    let position = position_qty(&engine, id);
    engine.end();

    let adapter = take_adapter_state();
    let stale_stop = adapter_detail("stale_stop");
    let fresh_stop = adapter_detail("fresh_stop");
    let fill_events = adapter_actions("fill");
    let stale_fill_count = fill_events
        .iter()
        .filter(|fill| fill["client_order_id"] == "STALE-STOP")
        .count();
    let outlier_submissions = adapter["orders"]
        .as_array()
        .map(|orders| {
            orders
                .iter()
                .filter(|order| order["client_order_id"] == "STALE-STOP")
                .count()
        })
        .unwrap_or(0);
    let pass = stale_stop["response"] == "STALE_POSITION_EPOCH"
        && stale_stop["venue_submission"] == false
        && stale_native_absent
        && outlier_submissions == 0
        && stale_fill_count == 0
        && fresh_stop["response"] == "ACCEPTED"
        && adapter["position_epoch"] == 2
        && close(position, 1.0);

    Ok(json!({
        "fixture": "F07",
        "scenario": "ADAPTER_OVER_NATIVE",
        "kind": "epoch_safety",
        "profile": adapter::PROFILE,
        "setup": {"open_epoch": 1, "reopen_epoch": 2, "stale_request_epoch": 1, "qty": 1.0},
        "new_epoch_qty": position,
        "stale_stop_response": stale_stop["response"],
        "stale_fill_count": stale_fill_count,
        "position_epoch": adapter["position_epoch"],
        "requested_epoch": stale_stop["requested_epoch"],
        "adapter": {
            "stale_stop": stale_stop,
            "fresh_stop": fresh_stop,
            "stale_order_registered": outlier_submissions > 0,
            "venue_submissions": adapter["venue_submissions"],
            "position_qty": adapter["position_qty"],
            "fills": fill_events,
            "epoch_sequence": ["E-1", "E-2", "E-3"],
        },
        "native": {
            "epoch_1_fill": epoch_1_fill,
            "close_fill": close_fill,
            "reopen_fill": reopen_fill,
            "fresh_stop_after_submit": fresh_after_submit,
            "stale_stop_present": !stale_native_absent,
            "native_control_after_submit": native_control_after_submit,
        },
        "native_limitation": "nautilus_model::position::Position has no epoch/position_epoch field and OrderMatchingEngine has no stale-epoch comparison; STALE-NATIVE is accepted by the venue with epoch-1 semantics",
        "profile_boundary": {
            "adapter_managed": "monotonic position epoch, stamped on open/reopen, checked before any stop reaches the venue",
            "native_gap": "no epoch field on Position and no stale-entity rejection path or error code",
        },
        "verdict": if pass { "PASS" } else { "FAIL" },
        "verdict_reason": if pass {
            "stale epoch stop rejected with STALE_POSITION_EPOCH before venue submission; fresh epoch stop accepted; the same stop sent natively is accepted, proving the native gap"
        } else {
            "epoch accounting, stale rejection or venue isolation not satisfied"
        },
        "pass": pass,
    }))
}

/// F29 over the adapter: a book-delta sequence guard keeps `last_good_seq`,
/// invalidates the client book on a gap, blocks new risk and only forwards
/// validated deltas to the native `OrderBook`.
fn probe_f29_adapter() -> Result<Value> {
    let instrument = instrument();
    let id = instrument.id();
    let mut book = OrderBook::new(id, BookType::L2_MBP);
    let mut adapter = Adapter::new();

    // Snapshot at last_update_id 100 is a client fact; the same levels are
    // loaded into the native book as the record of the market.
    adapter.book.snapshot(100);
    let seed_bid = OrderBookDelta::new(
        id,
        BookAction::Add,
        BookOrder::new(OrderSide::Buy, Price::from("100.00"), Quantity::from("100.000"), 1),
        0,
        100,
        ts(1),
        ts(1),
    );
    let seed_ask = OrderBookDelta::new(
        id,
        BookAction::Add,
        BookOrder::new(OrderSide::Sell, Price::from("101.00"), Quantity::from("10.000"), 2),
        0,
        100,
        ts(1),
        ts(1),
    );
    let seed_errors = vec![
        book.apply_delta(&seed_bid).err().map(|error| error.to_string()),
        book.apply_delta(&seed_ask).err().map(|error| error.to_string()),
    ];
    // Levels are stored at the delta quantity precision (3 decimal places).
    let bid_level_at_snapshot = book
        .get_quantity_at_level(Price::from("100.00"), OrderSide::Sell, 3)
        .as_f64();

    // Deltas: [first, last, previous] mirrors the fixture's BOOK_SEQUENCE steps.
    let steps: [(u64, u64, u64, &str, &str, &str); 2] = [
        (99, 101, 98, "60.000", "100.00", "Buy"),
        (102, 102, 101, "8.000", "101.00", "Sell"),
    ];
    let mut applied_stream: Vec<Value> = Vec::new();
    let mut apply_errors: Vec<Option<String>> = Vec::new();
    for (first, last, previous, qty, price, side) in steps {
        let verdict = adapter.book.delta(first, last, previous);
        let native_error = if verdict == adapter::BookVerdict::Applied {
            let delta = OrderBookDelta::new(
                id,
                BookAction::Update,
                BookOrder::new(
                    if side == "Buy" { OrderSide::Buy } else { OrderSide::Sell },
                    Price::from(price),
                    Quantity::from(qty),
                    1,
                ),
                0,
                last,
                ts(last),
                ts(last),
            );
            Some(book.apply_delta(&delta).err().map(|error| error.to_string()))
        } else {
            None
        };
        apply_errors.push(native_error.flatten());
        applied_stream.push(json!({
            "first": first,
            "last": last,
            "previous": previous,
            "adapter_verdict": if verdict == adapter::BookVerdict::Applied { "APPLIED" } else { "REJECTED" },
            "forwarded_to_native_book": verdict == adapter::BookVerdict::Applied,
        }));
    }

    let sequence_before_bad = book.sequence;
    let bid_level_before = book
        .get_quantity_at_level(Price::from("100.00"), OrderSide::Sell, 3)
        .as_f64();
    let ask_level_before = book
        .get_quantity_at_level(Price::from("101.00"), OrderSide::Buy, 3)
        .as_f64();

    // The bad delta: a 103 range whose previous sequence (999) does not chain.
    let initial_match = adapter.book.initial_match;
    let last_good_at_rejection = adapter.book.last_good_seq;
    let anchor_at_rejection = adapter.book.anchor;
    let bad_verdict = adapter.book.delta(103, 103, 999);
    let state_at_rejection = adapter.book.snapshot_state();
    let rejected_count = adapter.book.rejected;
    let applied_count = adapter.book.applied;
    let bad_forwarded = bad_verdict == adapter::BookVerdict::Applied;
    let blocked = adapter.gate_new_risk("F29-NEW");
    let rejected_delta = json!({
        "first": 103,
        "last": 103,
        "previous": 999,
        "adapter_verdict": "REJECTED",
        "code": adapter.book.last_reject.as_ref().map_or("", |reject| reject.code),
        "forwarded_to_native_book": bad_forwarded,
    });

    let sequence_after_bad = book.sequence;
    let bid_99_level = book
        .get_quantity_at_level(Price::from("99.00"), OrderSide::Sell, 3)
        .as_f64();
    let bid_level_after = book
        .get_quantity_at_level(Price::from("100.00"), OrderSide::Sell, 3)
        .as_f64();

    // Resync: a fresh snapshot re-validates the feed and unblocks risk.
    adapter.book.snapshot(110);
    let unblocked = adapter.gate_new_risk("F29-NEW");
    let resync_delta = adapter.book.delta(111, 111, 110);
    let state_after_resync = adapter.book.snapshot_state();

    let absolute_updates_not_additive =
        close(bid_level_after, 60.0) && !close(bid_level_after, bid_level_at_snapshot + 60.0);
    let native_untouched = sequence_after_bad == sequence_before_bad
        && sequence_after_bad == 102
        && close(bid_99_level, 0.0)
        && close(bid_level_after, bid_level_before);
    let pass = initial_match
        && last_good_at_rejection == Some(102)
        && state_at_rejection == "INVALID"
        && absolute_updates_not_additive
        && blocked.is_err()
        && bad_verdict == adapter::BookVerdict::Rejected
        && !bad_forwarded
        && native_untouched
        && unblocked.is_ok()
        && resync_delta == adapter::BookVerdict::Applied;

    Ok(json!({
        "fixture": "F29",
        "scenario": "ADAPTER_OVER_NATIVE",
        "kind": "book_sequence",
        "profile": adapter::PROFILE,
        "sequence_stream": [100, 101, 102, 110, 105],
        "initial_match": initial_match,
        "last_good_seq": last_good_at_rejection.map(|seq| seq.to_string()),
        "book_state": state_at_rejection,
        "absolute_updates_not_additive": absolute_updates_not_additive,
        "adapter": {
            "anchor_at_rejection": anchor_at_rejection,
            "last_good_seq_at_rejection": last_good_at_rejection,
            "applied": applied_count,
            "rejected": rejected_count,
            "applied_stream": applied_stream,
            "rejected_delta": rejected_delta,
            "risk_gate": {
                "code": blocked.err(),
                "blocked_risk_requests": adapter.blocked_risk_requests,
                "risk_events": adapter.risk_events,
            },
            "resync": {
                "snapshot": 110,
                "state_after": state_after_resync,
                "risk_allowed": unblocked.is_ok(),
                "delta_after_resync": if resync_delta == adapter::BookVerdict::Applied { "APPLIED" } else { "REJECTED" },
            },
        },
        "native_book": {
            "seed_errors": seed_errors,
            "apply_errors": apply_errors,
            "bid_level_qty_at_snapshot": bid_level_at_snapshot,
            "sequence_before_rejected_delta": sequence_before_bad,
            "sequence_after_rejected_delta": sequence_after_bad,
            "bid_level_qty_before": bid_level_before,
            "bid_level_qty_after": bid_level_after,
            "ask_level_qty_after": ask_level_before,
            "bid_99_level_qty": bid_99_level,
            "rejected_delta_applied": false,
            "note": "the rejected delta is never forwarded to OrderBook::apply_delta, so the native book keeps sequence 102 and no 99.00 level",
        },
        "native_limitation": "OrderBook exposes `sequence` as a high-water mark only: there is no snapshot anchor, no last_good_seq, no INVALID state and apply_delta/apply_deltas never validate continuity (crates/model/src/orderbook/book.rs)",
        "profile_boundary": {
            "adapter_managed": "book-delta sequence high-water mark, INVALID/RESYNC_REQUIRED gate, last_good_seq retention and snapshot-based resync",
            "native_gap": "no last_good_seq, book_state or gap validation in the public book API",
        },
        "verdict": if pass { "PASS" } else { "FAIL" },
        "verdict_reason": if pass {
            "sequence gap invalidates the client book at last_good_seq=102, risk is blocked until a snapshot resync, and the rejected delta never mutates the native book"
        } else {
            "sequence guard, risk gate, resync or native-book isolation not satisfied"
        },
        "pass": pass,
    }))
}

/// F31 over the adapter: the adapter stamps a venue sequence on order/fill
/// events, suppresses the duplicate fill and the out-of-order ACK, and zeroes
/// canceled leaves locally while the native order keeps its leaves quantity.
fn probe_f31_adapter() -> Result<Value> {
    take_adapter_state();
    let instrument = instrument();
    let id = instrument.id();
    let mut engine = new_engine()?;
    engine.add_venue(venue_config(BookType::L1_MBP, false, true))?;
    engine.add_instrument(&instrument)?;
    engine.add_strategy(Passive::new(id, Mode::AdapterF31, "F31A"))?;

    // t=1 submit buy 2 @ 100; t=2 a 1-lot trade partially fills the order.
    run_chunk(
        &mut engine,
        vec![
            quote(id, "100.00", "100.10", "10.000", "10.000", 1),
            trade(id, "100.00", "1.000", AggressorSide::Seller, 2, "F31A-T1"),
        ],
    )?;
    let native_after_partial = order_snapshot(&engine, "O-1");
    // t=4: delayed ACK (seq 1), duplicate fill (seq 2 / same trade id), cancel.
    run_chunk(
        &mut engine,
        vec![quote(id, "100.00", "100.10", "10.000", "10.000", 4)],
    )?;
    let native_after_cancel = order_snapshot(&engine, "O-1");
    engine.end();

    let adapter = take_adapter_state();
    let local = adapter_order(&adapter, "O-1");
    let accepted = num(&local, "quantity").unwrap_or(-1.0);
    let filled = num(&local, "filled_qty").unwrap_or(-1.0);
    let leaves = num(&local, "leaves_qty").unwrap_or(-1.0);
    let canceled = num(&local, "canceled_qty").unwrap_or(-1.0);
    let state = local["state"].as_str().unwrap_or("").to_string();
    let fill_events = adapter_actions("fill");
    let delayed_ack = adapter_detail("delayed_ack");
    let duplicate = adapter_detail("duplicate_fill");
    let cancel_arrived = adapter_detail("cancel_arrived");
    let native_leaves = num(&native_after_cancel, "leaves_qty").unwrap_or(-1.0);
    let duplicate_fee_debits = adapter["duplicate_fee_debits"].as_u64().unwrap_or(1);
    let strategy_fill_events = adapter["strategy_fill_events"].as_u64().unwrap_or(0);
    let pass = close(accepted, 2.0)
        && close(filled, 1.0)
        && close(leaves, 0.0)
        && close(canceled, 1.0)
        && state == "CANCELED"
        && duplicate_fee_debits == 0
        && strategy_fill_events == 1
        && delayed_ack["verdict"] == "OUT_OF_ORDER_SUPPRESSED"
        && duplicate["verdict"] == "DUPLICATE_SUPPRESSED"
        && cancel_arrived["response"] == "CANCELED";

    Ok(json!({
        "fixture": "F31",
        "scenario": "ADAPTER_OVER_NATIVE",
        "kind": "duplicate_fill",
        "profile": adapter::PROFILE,
        "accepted_qty": accepted,
        "filled_qty": filled,
        "leaves_qty": leaves,
        "canceled_qty": canceled,
        "state": state.to_uppercase(),
        "duplicate_funding_or_fee_debits": duplicate_fee_debits,
        "strategy_fill_events": strategy_fill_events,
        "adapter": {
            "local_order": local,
            "event_trace": {
                "fill": fill_events,
                "delayed_ack": delayed_ack,
                "duplicate_fill": duplicate,
                "cancel_arrived": cancel_arrived,
            },
            "duplicate_fills_suppressed": adapter["duplicate_fills_suppressed"],
            "out_of_order_events_suppressed": adapter["out_of_order_events_suppressed"],
            "last_venue_seq": adapter["last_venue_seq"],
            "venue_seq_source": "adapter stamp: the pinned engine has no venue sequence on OrderAccepted/OrderFilled",
        },
        "native": {
            "after_partial": native_after_partial,
            "after_cancel": native_after_cancel,
            "leaves_qty_after_cancel": native_leaves,
            "venue_seq_field_present": false,
            "local_order_event_hook_present": false,
        },
        "fixture_expects_leaves_qty": 0.0,
        "native_leaves_qty_after_cancel": native_leaves,
        "native_limitation": "OrderFilled/OrderAccepted carry no venue_seq and BacktestEngine/Data exposes no LOCAL_ORDER_EVENT hook; the native order keeps leaves_qty=1 after a cancel",
        "profile_boundary": {
            "adapter_managed": "venue sequence stamping, duplicate-fill dedupe by (order id, venue sequence, trade id), out-of-order ACK suppression, canceled leaves zeroed in client state",
            "native_gap": "no venue_seq on order events, no LOCAL_ORDER_EVENT injection, leaves_qty not zeroed on cancel",
        },
        "verdict": if pass { "PASS" } else { "FAIL" },
        "verdict_reason": if pass {
            "exactly one strategy-visible fill; duplicate fill and delayed ACK suppressed with no fee debit; client canceled leaves are zero while the native order keeps leaves=1"
        } else {
            "dedupe, sequencing, fee or canceled-leaves accounting not satisfied"
        },
        "pass": pass,
    }))
}

/// F06 client profile: an atomic client OCO group sizes reduce-only exits from
/// the filled entry, cancels the sibling on the first fill and rejects a
/// second exit while flat. Native OCO is untouched.
fn probe_f06_client_adapter() -> Result<Value> {
    take_adapter_state();
    let instrument = instrument();
    let id = instrument.id();
    let mut engine = new_engine()?;
    engine.add_venue(venue_config(BookType::L1_MBP, false, true))?;
    engine.add_instrument(&instrument)?;
    engine.add_strategy(Passive::new(id, Mode::AdapterClientOco, "F06C"))?;

    // t=1 entry fills 1 @ 100.00.
    run_chunk(
        &mut engine,
        vec![quote(id, "99.90", "100.00", "10.000", "10.000", 1)],
    )?;
    let entry = order_snapshot(&engine, "GR-ENTRY");
    // t=2 bracket installed on the filled quantity only.
    run_chunk(
        &mut engine,
        vec![quote(id, "99.90", "100.00", "10.000", "10.000", 2)],
    )?;
    let tp_after_install = order_snapshot(&engine, "GR-TP");
    let sl_after_install = order_snapshot(&engine, "GR-SL");
    // t=3 price trades through the take profit; the sibling must be canceled.
    run_chunk(
        &mut engine,
        vec![quote(id, "102.00", "102.50", "10.000", "10.000", 3)],
    )?;
    let tp_after_fill = order_snapshot(&engine, "GR-TP");
    let sl_after_sibling_cancel = order_snapshot(&engine, "GR-SL");
    let position_after_tp = position_signed_qty(&engine, id);
    // t=4 second child eligible while flat: adapter guard + native control.
    run_chunk(
        &mut engine,
        vec![quote(id, "102.00", "102.50", "10.000", "10.000", 4)],
    )?;
    let second_child_native = order_snapshot(&engine, "GR-SL2");
    // t=5 retire the native control order.
    run_chunk(
        &mut engine,
        vec![quote(id, "102.00", "102.50", "10.000", "10.000", 5)],
    )?;
    let position = position_signed_qty(&engine, id);
    let open_positions = open_position_count(&engine, id);
    engine.end();

    let adapter = take_adapter_state();
    let guard = adapter_detail("reduce_only_guard");
    let bracket = adapter_detail("bracket_registered");
    let sibling_cancel = adapter_detail("group_sibling_cancel");
    let sibling_canceled = adapter_detail("group_sibling_canceled");
    let entry_filled = num(&bracket, "entry_filled_qty").unwrap_or(-1.0);
    let exit_qty = num(&bracket, "tp_qty").unwrap_or(-1.0);
    let tp_filled = adapter_order(&adapter, "GR-TP")
        .get("filled_qty")
        .and_then(Value::as_f64)
        .unwrap_or(-1.0);
    let oco_fill_count = adapter["orders"]
        .as_array()
        .map(|orders| {
            orders
                .iter()
                .filter(|order| order["group"] == "GR-1" && num(order, "filled_qty").unwrap_or(0.0) > 0.0)
                .count()
        })
        .unwrap_or(0);
    let overfill = tp_filled > entry_filled + 1e-9;
    let position_reversed = position < 0.0;
    let pass = close(entry_filled, 1.0)
        && close(exit_qty, 1.0)
        && close(tp_filled, 1.0)
        && oco_fill_count == 1
        && !overfill
        && !position_reversed
        && close(position, 0.0)
        && guard["response"] == "REDUCE_ONLY_REJECT"
        && guard["venue_submission"] == false
        && sibling_cancel["policy"] == "ATOMIC_CLIENT_OCO"
        && sibling_canceled["response"] == "CANCELED"
        && sl_after_sibling_cancel["status"] == "Canceled";

    Ok(json!({
        "fixture": "F06",
        "scenario": "CLIENT_REDUCE_ONLY",
        "kind": "client_managed_oco_safe",
        "profile": adapter::PROFILE,
        "entry_filled_qty": entry_filled,
        "exit_total_qty": tp_filled,
        "exit_qty_sized_from_filled": exit_qty,
        "oco_fill_count": oco_fill_count,
        "fill_count": oco_fill_count,
        "position_qty": position,
        "position_reversed": position_reversed,
        "overfill": overfill,
        "second_child_response": guard["response"],
        "adapter": {
            "bracket": bracket,
            "group_sibling_cancel": sibling_cancel,
            "group_sibling_canceled": sibling_canceled,
            "reduce_only_guard": guard,
            "orders": adapter["orders"],
            "group_sibling_cancels": adapter["group_sibling_cancels"],
            "strategy_fill_events": adapter["strategy_fill_events"],
        },
        "native": {
            "entry": entry,
            "tp_after_install": tp_after_install,
            "sl_after_install": sl_after_install,
            "tp_after_fill": tp_after_fill,
            "sl_after_sibling_cancel": sl_after_sibling_cancel,
            "second_child_native_control": second_child_native,
            "position_signed_qty_after_tp": position_after_tp,
            "open_positions": open_positions,
            "note": "the sibling cancel is client-initiated after the fill; native OCO (F06 NATIVE_ATOMIC) performs it autonomously and is unchanged",
        },
        "native_oco_unchanged": true,
        "native_limitation": "the OCO contingency group only cancels a sibling for a native bracket submitted as an order list; a client-managed group has no group/epoch advisory and no both-legs-eligible event",
        "profile_boundary": {
            "adapter_managed": "atomic client group, sibling cancel on first fill, exits sized from the filled entry, reduce-only exit rejected while flat",
            "native_gap": "no client group state, no group-level atomic cancel and no reduce-only rejection advisory for a client-managed pair",
        },
        "verdict": if pass { "PASS" } else { "FAIL" },
        "verdict_reason": if pass {
            "one client OCO fill for a 1-lot entry, sibling canceled atomically, zero overfill and no position reversal; native OCO unchanged"
        } else {
            "client OCO sizing, sibling cancellation, overfill or position invariant not satisfied"
        },
        "pass": pass,
    }))
}

/// F02 over the adapter: an unknown-aggressor trade does not deplete the
/// client queue. The native engine's own behavior for the same trade is
/// recorded next to it as the profile difference.
fn probe_unknown_aggressor() -> Result<Value> {
    take_adapter_state();
    let instrument = instrument();
    let id = instrument.id();
    let mut engine = new_engine()?;
    engine.add_venue(venue_config(BookType::L1_MBP, true, true))?;
    engine.add_instrument(&instrument)?;
    engine.add_strategy(Passive::new(id, Mode::AdapterUnknownAggressor, "UA"))?;

    run_chunk(
        &mut engine,
        vec![quote(id, "100.00", "101.00", "100.000", "10.000", 1)],
    )?;
    let after_submit = order_snapshot(&engine, "O-1");
    run_chunk(
        &mut engine,
        vec![trade(id, "100.00", "80.000", AggressorSide::Seller, 2, "UA-T1")],
    )?;
    let after_seller_trade = order_snapshot(&engine, "O-1");
    run_chunk(
        &mut engine,
        vec![trade(id, "100.00", "30.000", AggressorSide::NoAggressor, 3, "UA-T2")],
    )?;
    let after_unknown_trade = order_snapshot(&engine, "O-1");
    engine.end();

    let adapter = take_adapter_state();
    let trades = adapter_actions("trade");
    let known_trade = trades.first().cloned().unwrap_or(Value::Null);
    let unknown_trade = trades.last().cloned().unwrap_or(Value::Null);
    let native_filled = num(&after_unknown_trade, "filled_qty").unwrap_or(-1.0);
    let native_fill_on_unknown = native_filled
        - num(&after_seller_trade, "filled_qty").unwrap_or(0.0);
    let local = adapter_order(&adapter, "O-1");
    let adapter_fill_on_unknown = num(&unknown_trade, "fill_qty").unwrap_or(-1.0);
    let ahead_after = num(&unknown_trade, "ahead_after").unwrap_or(-1.0);
    let policy_applied = unknown_trade["policy"] == "CONSERVATIVE_NO_DEPLETION"
        && close(adapter_fill_on_unknown, 0.0)
        && close(ahead_after, 20.0);
    let profile_difference = native_fill_on_unknown > 1e-9;
    let pass = policy_applied && close(num(&known_trade, "ahead_after").unwrap_or(-1.0), 20.0);

    Ok(json!({
        "fixture": "F02+UNKNOWN_AGGRESSOR",
        "scenario": "ADAPTER_OVER_NATIVE",
        "kind": "unknown_aggressor_queue",
        "profile": adapter::PROFILE,
        "adapter": {
            "trades": trades,
            "ahead_after_known_trade": known_trade["ahead_after"],
            "ahead_after_unknown_trade": ahead_after,
            "fill_on_unknown_aggressor_qty": adapter_fill_on_unknown,
            "policy": unknown_trade["policy"],
            "local_order": local,
            "queue_depletions_skipped": adapter["queue_depletions_skipped"],
            "unknown_aggressor_trades": adapter["unknown_aggressor_trades"],
        },
        "native": {
            "after_submit": after_submit,
            "after_seller_trade": after_seller_trade,
            "after_unknown_trade": after_unknown_trade,
            "filled_qty_after_unknown_trade": native_filled,
            "fill_on_unknown_aggressor_qty": native_fill_on_unknown,
        },
        "native_fill_on_unknown_aggressor_qty": native_fill_on_unknown,
        "profile_difference": profile_difference,
        "native_limitation": "the native matching engine decrements queue-ahead for AggressorSide::NoAggressor and forces both sides to the trade price (crates/execution/src/matching_engine/engine.rs), so it fills where the conservative client model does not",
        "profile_boundary": {
            "adapter_managed": "conservative no-depletion policy on unknown aggressor trades",
            "native_gap": "the native engine treats NoAggressor as a two-sided trade; the adapter does not reproduce that because the print cannot be attributed to the resting side",
        },
        "verdict": if pass && !profile_difference { "PASS" } else if pass { "PARTIAL" } else { "FAIL" },
        "verdict_reason": if !pass {
            "conservative queue policy not applied"
        } else if profile_difference {
            "adapter queue model is conservative and does not deplete on the unknown aggressor; the native engine filled on the same trade, which is recorded as the profile difference"
        } else {
            "adapter conservative policy applied and the native engine also did not fill on the unknown aggressor"
        },
        "pass": pass,
    }))
}

fn blocked_fixtures() -> Value {
    json!({
        "F07": {
            "title": "Epoch safety",
            "verdict": "BLOCKED_BACKEND",
            "missing_api": "nautilus_model::position::Position has no epoch field and OrderMatchingEngine has no stale-epoch rejection path",
            "missing_field": "Position has no epoch/position_epoch field (crates/model/src/position.rs Position fields include opening_order_id, closing_order_id, ts_opened, ts_closed, trade_ids)",
            "missing_error": "no STALE_POSITION_EPOCH code or stale-epoch comparison exists under crates/ (grep over crates/**/*.rs returns only websocket-connection and adapter-specific epochs)",
            "checked_symbols": ["nautilus_model::position::Position", "OrderFilled", "OrderMatchingEngine::process_order", "OrderMatchingEngine::process_cancel"],
            "evidence": "crates/model/src/position.rs Position struct: opening_order_id, ts_opened, ts_closed; no epoch. crates/execution/src/matching_engine/engine.rs has no epoch comparison.",
            "claim": "stale stop with STALE_POSITION_EPOCH response cannot be produced or observed"
        },
        "F31": {
            "title": "Delayed ACK, duplicate fill and canceled qty",
            "verdict": "BLOCKED_BACKEND",
            "missing_api": "no backtest API to inject LOCAL_ORDER_EVENT / out-of-order ACK / duplicate fill into the local OMS",
            "evidence": "Order::is_duplicate_fill exists (crates/model/src/orders/mod.rs) but there is no public injection path through BacktestEngine",
            "claim": "duplicate-fill suppression cannot be exercised natively; only the order-level predicate exists"
        },
        "F29": {
            "title": "Snapshot anchor and renewal",
            "verdict": "BLOCKED_BACKEND",
            "missing_api": "OrderBook.sequence is a public counter but there is no snapshot-anchor, sequence-gap or INVALID-book state API",
            "evidence": "crates/model/src/orderbook/book.rs exposes sequence and increment(); apply_deltas applies deltas without gap validation or book invalidation",
            "claim": "book_state INVALID and last_good_seq are not observable",
            "probe": "probes.f29_book_sequence feeds sequence 100,101,102,110,105: the forward gap and the out-of-order delta are both accepted with no error"
        },
        "F29_FIELDS": {
            "title": "Missing book fields",
            "verdict": "BLOCKED_BACKEND",
            "missing_field": "last_good_seq, book_state (VALID/INVALID), snapshot anchor / last_update_id",
            "present_field": "OrderBook.sequence (high-water mark only)"
        },
        "F31_FIELDS": {
            "title": "Missing event-ordering fields",
            "verdict": "BLOCKED_BACKEND",
            "missing_field": "venue_seq on OrderFilled/OrderAccepted; BacktestEngine/Data LOCAL_ORDER_EVENT hook",
            "present_hook": "ExecutionEngine::process(&OrderEventAny) for duplicate-fill suppression (probes.f31_duplicate_fill)"
        }
    })
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

fn main() -> Result<()> {
    let out_path = std::env::args().nth(1);
    let started = Instant::now();

    let f02 = probe(probe_f02);
    let f02_cancel = probe(probe_f02_cancel);
    let f02_depth = probe(probe_f02_depth_once);
    let shared = probe(probe_shared_level);
    let f03 = probe(probe_f03_shared_liquidity);
    let f33_fok = probe(probe_f33_fok);
    let f33_gap = probe(probe_f33_stop_gap);
    let f04 = probe(probe_f04_cancel_race);
    let f05 = probe(probe_f05_replace);
    let f34 = probe(probe_f34_partial_entry);
    let f06 = probe(probe_f06_oco);
    let f29 = probe(probe_f29_book_sequence);
    let f31 = probe(probe_f31_duplicate_fill);
    let client_oco = probe(probe_client_oco_overfill);

    // Adapter-over-native probes (client order-management layer).
    let adapter_f04 = probe(probe_f04_adapter);
    let adapter_f05 = probe(probe_f05_adapter);
    let adapter_f07 = probe(probe_f07_adapter);
    let adapter_f29 = probe(probe_f29_adapter);
    let adapter_f31 = probe(probe_f31_adapter);
    let adapter_f06 = probe(probe_f06_client_adapter);
    let adapter_unknown = probe(probe_unknown_aggressor);

    let summary = json!({
        "F02": f02["verdict"],
        "F02+CANCEL_REMAINDER": f02_cancel["verdict"],
        "F02+depth-once": f02_depth["verdict"],
        "F02+SHARED_LEVEL": shared["verdict"],
        "F03": f03["verdict"],
        "F04": f04["verdict"],
        "F05": f05["verdict"],
        "F06": f06["verdict"],
        "F07": "BLOCKED_BACKEND",
        "F33_FOK": f33_fok["verdict"],
        "F33_STOP_LIMIT_GAP": f33_gap["verdict"],
        "F34": f34["verdict"],
        "F29": f29["verdict"],
        "F31": f31["verdict"],
        "F06_CLIENT_UNSAFE_OBSERVABLE": client_oco["verdict"],
    });

    let adapter_summary = json!({
        "profile": adapter::PROFILE,
        "F04": adapter_f04["verdict"],
        "F05": adapter_f05["verdict"],
        "F06_CLIENT_REDUCE_ONLY": adapter_f06["verdict"],
        "F07": adapter_f07["verdict"],
        "F29": adapter_f29["verdict"],
        "F31": adapter_f31["verdict"],
        "F02+UNKNOWN_AGGRESSOR": adapter_unknown["verdict"],
    });

    let document = json!({
        "probe": "qel-passive-queue-runner",
        "scope": "#10 / TASK-17",
        "pinned": {
            "repo": "https://github.com/nautechsystems/nautilus_trader",
            "tag": "v1.231.0",
            "commit": "27a8e54e7ac3c57d6cbf8891f0283dfbaee97317"
        },
        "elapsed_ms": started.elapsed().as_millis() as u64,
        "summary": summary,
        "adapter_summary": adapter_summary,
        "probes": {
            "f02_queue": f02,
            "f02_cancel_remainder": f02_cancel,
            "f02_depth_once": f02_depth,
            "f02_shared_level": shared,
            "f03_shared_liquidity": f03,
            "f33_fok": f33_fok,
            "f33_stop_limit_gap": f33_gap,
            "f04_cancel_race": f04,
            "f05_replace": f05,
            "f34_partial_entry": f34,
            "f06_oco": f06,
            "f29_book_sequence": f29,
            "f31_duplicate_fill": f31,
            "f06_client_unsafe_observable": client_oco,
            "adapter_f04_cancel_race": adapter_f04,
            "adapter_f05_replace": adapter_f05,
            "adapter_f06_client_oco": adapter_f06,
            "adapter_f07_epoch": adapter_f07,
            "adapter_f29_book_sequence": adapter_f29,
            "adapter_f31_duplicate_fill": adapter_f31,
            "adapter_f02_unknown_aggressor": adapter_unknown
        },
        "adapter_profile": {
            "name": adapter::PROFILE,
            "definition": "client order-management layer over the pinned native matching engine; native matching remains the exchange",
            "claimed": [
                "explicit ALREADY_FILLED cancel response from local terminal state (F04)",
                "client-managed cancel+new replace with priority_reset_count (F05)",
                "monotonic position epoch and STALE_POSITION_EPOCH rejection before the venue (F07)",
                "book-delta sequence high-water mark with INVALID/RESYNC_REQUIRED and last_good_seq (F29)",
                "venue sequence stamping, duplicate-fill and out-of-order ACK suppression, zeroed canceled leaves (F31)",
                "atomic client OCO sibling cancel with reduce-only exits sized from the filled entry (F06 client profile)",
                "conservative no-depletion queue policy on unknown-aggressor trades"
            ],
            "not_claimed": [
                "any of these as a native venue capability",
                "order-book matching fidelity from the adapter",
                "client-server persistence or restart durability of adapter state",
                "native queue-state readback or native priority_reset_count",
                "native venue sequence on order events",
                "empty-book resync from a real venue snapshot feed",
                "unknown-aggressor fill parity with the native engine"
            ]
        },
        "blocked_fixtures": blocked_fixtures(),
        "acceptance_boundary": {
            "claimed": "CAP-L2-QUEUE PARTIAL: passive maker queue-ahead on the F02 slice, trade-driven queue depletion, depth-once on L2",
            "not_claimed": [
                "queue state readback",
                "shared level budget across multiple passive orders unless F02+SHARED_LEVEL passes",
                "unknown aggressor handling",
                "resync after book gaps",
                "full L2 depth semantics beyond the tested level",
                "mark-based liquidation / LocalView / CAP-ISOLATED",
                "F07 position epochs",
                "F29 last_good_seq and INVALID book state",
                "F31 delayed/out-of-order ACK venue sequencing",
                "native F04 ALREADY_FILLED cancel response and native F05 priority_reset_count (adapter-managed in this evidence; the native gaps remain)"
            ],
            "adapter_note": "the adapter profile above answers F04, F05, F07, F29, F31 and the F06 client profile as client-owned semantics; it does not change any native capability verdict and CAP-L2-QUEUE remains PARTIAL"
        }
    });

    let text = serde_json::to_string_pretty(&document)?;
    if let Some(path) = out_path {
        fs::write(&path, &text)?;
    }
    println!("{text}");
    Ok(())
}
