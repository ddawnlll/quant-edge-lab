//! QEL functional capability spike against pinned NautilusTrader v1.231.0.
//!
//! Scope: issues #3, #4, #10 and #11. This binary answers one question only:
//! does the public backtest API actually expose the queue and liquidation hooks
//! that the read-only source probe found?
//!
//! It is a functional spike, not a production integration. Nothing here is
//! imported by the production modules.

use std::fmt::Debug;
use std::time::Instant;

use nautilus_backtest::{
    config::{BacktestEngineConfig, SimulatedVenueConfig},
    engine::BacktestEngine,
};
use nautilus_common::{
    actor::DataActor,
    logging::config::LoggerConfig,
};
use nautilus_core::UnixNanos;
use nautilus_model::{
    accounts::{
        Account, AccountAny, margin_model::MarginModelAny, margin_model::StandardMarginModel,
    },
    data::{
        BookOrder, Data, FundingRateUpdate, MarkPriceUpdate, OrderBookDelta, OrderBookDeltas,
        QuoteTick, TradeTick,
    },
    enums::{
        AccountType, AggressorSide, BookAction, BookType, OmsType, OrderSide, OrderType,
    },
    identifiers::{InstrumentId, StrategyId, Symbol, TradeId, Venue},
    instruments::{Instrument, InstrumentAny, crypto_perpetual::CryptoPerpetual},
    orderbook::OrderBook,
    orders::{Order, OrderAny},
    types::{Currency, Money, Price, Quantity},
};
use nautilus_trading::{Strategy, StrategyConfig, StrategyCore, nautilus_strategy};
use rust_decimal::Decimal;
use serde_json::{Value, json};

const VENUE: &str = "BINANCE";
const MAINTENANCE_RATE: Decimal = Decimal::from_parts(5, 0, 0, false, 2); // 0.05

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

/// ETHUSDT perpetual with the fixture maintenance rate (0.05) and zero fees.
///
/// The stubbed `crypto_perpetual_ethusdt()` has `margin_maint = 0.35`, which
/// cannot reproduce the F11/F12 setup. This clone keeps the same id/precisions
/// but sets both margin rates to 0.05 and removes fees so cash moves are exact.
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

fn trade(
    id: InstrumentId,
    price: &str,
    qty: &str,
    aggressor: AggressorSide,
    t: u64,
    trade_id: &str,
) -> Data {
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

fn mark(id: InstrumentId, price: &str, t: u64) -> Data {
    Data::MarkPriceUpdate(MarkPriceUpdate::new(
        id,
        Price::from(price),
        ts(t),
        ts(t),
    ))
}

fn funding(
    id: InstrumentId,
    rate: &str,
    interval: u16,
    next_funding: u64,
    t: u64,
) -> Data {
    Data::FundingRateUpdate(FundingRateUpdate::new(
        id,
        rate.parse().unwrap(),
        Some(interval),
        Some(ts(next_funding)),
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

fn new_engine() -> anyhow::Result<BacktestEngine> {
    BacktestEngine::new(BacktestEngineConfig {
        logging: LoggerConfig {
            bypass_logging: true,
            ..Default::default()
        },
        ..Default::default()
    })
}

fn venue_config(
    balance: &str,
    book_type: BookType,
    queue_position: bool,
    liquidity_consumption: bool,
    liquidation_enabled: bool,
) -> SimulatedVenueConfig {
    SimulatedVenueConfig::builder()
        .venue(Venue::from(VENUE))
        .oms_type(OmsType::Netting)
        .account_type(AccountType::Margin)
        .book_type(book_type)
        .starting_balances(vec![Money::from(balance)])
        // StandardMarginModel: maintenance = notional * margin_maint, no
        // leverage division. This is the only model that can express the
        // fixture's 0.05 maintenance rate against the entry price.
        .margin_model(MarginModelAny::Standard(StandardMarginModel))
        .trade_execution(true)
        .queue_position(queue_position)
        .liquidity_consumption(liquidity_consumption)
        .liquidation_enabled(liquidation_enabled)
        .liquidation_trigger_ratio(1.0)
        .liquidation_cancel_open_orders(true)
        .build()
        .expect("valid simulated venue config")
}

// ---------------------------------------------------------------------------
// Strategies
// ---------------------------------------------------------------------------

struct OneShotLimit {
    core: StrategyCore,
    instrument_id: InstrumentId,
    quantity: Quantity,
    price: Price,
    submitted: bool,
}

impl OneShotLimit {
    fn new(instrument_id: InstrumentId, quantity: &str, price: &str) -> Self {
        Self {
            core: StrategyCore::new(StrategyConfig {
                strategy_id: Some(StrategyId::from("QUEUE-SPIKE-001")),
                order_id_tag: Some("001".to_string()),
                ..Default::default()
            }),
            instrument_id,
            quantity: Quantity::from(quantity),
            price: Price::from(price),
            submitted: false,
        }
    }

    fn submit(&mut self) -> anyhow::Result<()> {
        if self.submitted {
            return Ok(());
        }
        self.submitted = true;
        let order = self.order().limit(
            self.instrument_id,
            OrderSide::Buy,
            self.quantity,
            self.price,
            Some(nautilus_model::enums::TimeInForce::Gtc),
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        );
        self.submit_order(order, None, None, None)
    }
}

nautilus_strategy!(OneShotLimit);

impl Debug for OneShotLimit {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("OneShotLimit").finish()
    }
}

impl DataActor for OneShotLimit {
    fn on_start(&mut self) -> anyhow::Result<()> {
        self.subscribe_quotes(self.instrument_id, None, None);
        Ok(())
    }

    fn on_quote(&mut self, _quote: &QuoteTick) -> anyhow::Result<()> {
        self.submit()
    }
}

struct BookTriggerLimit {
    core: StrategyCore,
    instrument_id: InstrumentId,
    quantity: Quantity,
    price: Price,
    submitted: bool,
}

impl BookTriggerLimit {
    fn new(instrument_id: InstrumentId, quantity: &str, price: &str) -> Self {
        Self {
            core: StrategyCore::new(StrategyConfig {
                strategy_id: Some(StrategyId::from("QUEUE-SPIKE-L2-001")),
                order_id_tag: Some("001".to_string()),
                ..Default::default()
            }),
            instrument_id,
            quantity: Quantity::from(quantity),
            price: Price::from(price),
            submitted: false,
        }
    }
}

nautilus_strategy!(BookTriggerLimit);

impl Debug for BookTriggerLimit {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("BookTriggerLimit").finish()
    }
}

impl DataActor for BookTriggerLimit {
    fn on_start(&mut self) -> anyhow::Result<()> {
        self.subscribe_book_deltas(self.instrument_id, BookType::L2_MBP, None, None, false, None);
        Ok(())
    }

    fn on_book_deltas(&mut self, _deltas: &OrderBookDeltas) -> anyhow::Result<()> {
        if self.submitted {
            return Ok(());
        }
        self.submitted = true;
        let order = self.order().limit(
            self.instrument_id,
            OrderSide::Buy,
            self.quantity,
            self.price,
            Some(nautilus_model::enums::TimeInForce::Gtc),
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        );
        self.submit_order(order, None, None, None)
    }
}

struct OneShotMarket {
    core: StrategyCore,
    instrument_id: InstrumentId,
    quantity: Quantity,
    submitted: bool,
}

impl OneShotMarket {
    fn new(instrument_id: InstrumentId, quantity: &str) -> Self {
        Self {
            core: StrategyCore::new(StrategyConfig {
                strategy_id: Some(StrategyId::from("LIQ-SPIKE-001")),
                order_id_tag: Some("001".to_string()),
                ..Default::default()
            }),
            instrument_id,
            quantity: Quantity::from(quantity),
            submitted: false,
        }
    }
}

nautilus_strategy!(OneShotMarket);

impl Debug for OneShotMarket {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("OneShotMarket").finish()
    }
}

impl DataActor for OneShotMarket {
    fn on_start(&mut self) -> anyhow::Result<()> {
        self.subscribe_quotes(self.instrument_id, None, None);
        Ok(())
    }

    fn on_quote(&mut self, _quote: &QuoteTick) -> anyhow::Result<()> {
        if self.submitted {
            return Ok(());
        }
        self.submitted = true;
        let order = self.order().market(
            self.instrument_id,
            OrderSide::Buy,
            self.quantity,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        );
        self.submit_order(order, None, None, None)
    }
}

// ---------------------------------------------------------------------------
// Observation helpers
// ---------------------------------------------------------------------------

fn limit_order(engine: &BacktestEngine, instrument_id: InstrumentId) -> Option<OrderAny> {
    let cache = engine.kernel().cache.borrow();
    cache
        .orders(None, Some(&instrument_id), None, None, None)
        .into_iter()
        .find(|order| order.order_type() == OrderType::Limit)
        .map(|order| order.cloned())
}

fn liquidation_orders(engine: &BacktestEngine, instrument_id: InstrumentId) -> Vec<OrderAny> {
    let cache = engine.kernel().cache.borrow();
    cache
        .orders(None, Some(&instrument_id), None, None, None)
        .into_iter()
        .filter(|order| {
            order.order_type() == OrderType::Market
                && format!("{}", order.client_order_id()).starts_with("LIQUIDATION-")
        })
        .map(|order| order.cloned())
        .collect()
}

fn open_positions(engine: &BacktestEngine, instrument_id: InstrumentId) -> usize {
    let cache = engine.kernel().cache.borrow();
    cache
        .positions_open(None, Some(&instrument_id), None, None, None)
        .len()
}

fn closed_position(engine: &BacktestEngine, instrument_id: InstrumentId) -> Option<nautilus_model::position::Position> {
    let cache = engine.kernel().cache.borrow();
    cache
        .positions_closed(None, Some(&instrument_id), None, None, None)
        .into_iter()
        .next()
        .map(|position| position.cloned())
}

fn account_state(engine: &BacktestEngine) -> (f64, f64) {
    let cache = engine.kernel().cache.borrow();
    for account in cache.accounts_all_owned() {
        if let AccountAny::Margin(margin) = account {
            let total = margin
                .balance(Some(Currency::USDT()))
                .map_or(0.0, |balance| balance.total.as_f64());
            let maintenance = margin
                .total_maintenance_margin(Currency::USDT())
                .as_f64();
            return (total, maintenance);
        }
    }
    (0.0, 0.0)
}

fn upnl(engine: &BacktestEngine, instrument_id: InstrumentId) -> f64 {
    let cache = engine.kernel().cache.borrow();
    cache
        .positions_open(None, Some(&instrument_id), None, None, None)
        .first()
        .and_then(|position| cache.calculate_unrealized_pnl(position))
        .map_or(0.0, |money| money.as_f64())
}

fn order_snapshot(order: &OrderAny) -> Value {
    json!({
        "client_order_id": format!("{}", order.client_order_id()),
        "order_type": format!("{:?}", order.order_type()),
        "status": format!("{:?}", order.status()),
        "filled_qty": order.filled_qty().as_f64(),
        "leaves_qty": order.leaves_qty().as_f64(),
        "avg_px": order.avg_px().map(|px| px.to_string()),
    })
}

fn adjustments(position: &nautilus_model::position::Position) -> Vec<Value> {
    position
        .adjustments
        .iter()
        .map(|adjustment| {
            json!({
                "type": format!("{:?}", adjustment.adjustment_type),
                "pnl_change": adjustment.pnl_change.map(|money| money.as_f64()),
                "ts_event": adjustment.ts_event.as_u64(),
                "ts_init": adjustment.ts_init.as_u64(),
                "reason": adjustment.reason.map(|r| r.to_string()),
            })
        })
        .collect()
}

fn probe<F>(f: F) -> Value
where
    F: FnOnce() -> anyhow::Result<Value>,
{
    match f() {
        Ok(value) => value,
        Err(error) => json!({"error": format!("{error:#}")}),
    }
}

// ---------------------------------------------------------------------------
// F02: queue ahead 100, qualifying trades 80 then 30 -> 10 fill
// ---------------------------------------------------------------------------

fn run_f02_l1() -> anyhow::Result<Value> {
    let instrument = instrument();
    let id = instrument.id();
    let mut engine = new_engine()?;
    engine.add_venue(venue_config(
        "1000 USDT",
        BookType::L1_MBP,
        true,
        true,
        false,
    ))?;
    engine.add_instrument(&instrument)?;
    engine.add_strategy(OneShotLimit::new(id, "20.000", "100.00"))?;

    // Chunk 1: BBO bid 100.00 x 100.000, then a qualifying seller trade of 80.
    engine.add_data(
        vec![
            quote(id, "100.00", "101.00", "100.000", "10.000", 1),
            trade(id, "100.00", "80.000", AggressorSide::Seller, 2, "F02-T1"),
        ],
        None,
        true,
        true,
    )?;
    engine.run(None, None, None, true)?;
    engine.clear_data();
    let after_trade_1 = limit_order(&engine, id)
        .map(|order| order_snapshot(&order))
        .unwrap_or_else(|| json!({"error": "limit order not found after trade 1"}));

    // Chunk 2: second qualifying seller trade of 30. Queue 20 -> 0, excess 10.
    engine.add_data(
        vec![trade(
            id,
            "100.00",
            "30.000",
            AggressorSide::Seller,
            3,
            "F02-T2",
        )],
        None,
        true,
        true,
    )?;
    engine.run(None, None, None, true)?;
    engine.clear_data();
    engine.end();

    let after_trade_2 = limit_order(&engine, id)
        .map(|order| order_snapshot(&order))
        .unwrap_or_else(|| json!({"error": "limit order not found after trade 2"}));

    let filled_1 = after_trade_1["filled_qty"].as_f64().unwrap_or(-1.0);
    let filled_2 = after_trade_2["filled_qty"].as_f64().unwrap_or(-1.0);
    let fill_step_2 = filled_2 - filled_1;
    let leaves = after_trade_2["leaves_qty"].as_f64().unwrap_or(-1.0);

    Ok(json!({
        "fixture": "F02",
        "scenario": "BASE",
        "setup": {"ahead": 100.0, "order_qty": 20.0, "order_price": 100.0, "trades": [80.0, 30.0]},
        "api": {
            "venue_config": "SimulatedVenueConfig::builder().queue_position(true).liquidity_consumption(true).trade_execution(true)",
            "book_type": "L1_MBP",
            "exact_symbol": "fn snapshot_queue_position / fn decrement_queue_on_trade / fn apply_liquidity_consumption",
            "symbol_visibility": "private methods on OrderMatchingEngine; behavior reachable only through the venue config flags",
        },
        "after_trade_1": after_trade_1,
        "after_trade_2": after_trade_2,
        "filled_after_trade_1_qty": filled_1,
        "fill_step_2_qty": fill_step_2,
        "leaves_qty": leaves,
        "ahead_qty_after": if (fill_step_2 - 10.0).abs() < 1e-9 { 0.0 } else { f64::NAN },
        "queue_state_observable": false,
        "queue_state_note": "queue_ahead_total is a private OrderMatchingEngine field; ahead=0 is inferred from the exact 10.0 fill on the second trade, not read back",
        "expected": {"filled_after_trade_1_qty": 0.0, "fill_step_2_qty": 10.0, "leaves_qty": 10.0, "ahead_qty": 0.0},
        "pass": (filled_1 - 0.0).abs() < 1e-9 && (fill_step_2 - 10.0).abs() < 1e-9 && (leaves - 10.0).abs() < 1e-9,
    }))
}

// ---------------------------------------------------------------------------
// F02 depth-once: an L2 depth reduction after a trade must not double-consume
// ---------------------------------------------------------------------------

fn run_f02_l2_depth_once() -> anyhow::Result<Value> {
    let instrument = instrument();
    let id = instrument.id();
    let mut engine = new_engine()?;
    engine.add_venue(venue_config(
        "1000 USDT",
        BookType::L2_MBP,
        true,
        true,
        false,
    ))?;
    engine.add_instrument(&instrument)?;
    engine.add_strategy(BookTriggerLimit::new(id, "20.000", "100.00"))?;

    // Chunk 1: L2 snapshot (bid 100 x 100, ask 101 x 10), trade 80, then a
    // depth reduction to 20 that already reflects the trade.
    engine.add_data(
        vec![
            delta(id, BookAction::Add, OrderSide::Buy, "100.00", "100.000", 1, 1),
            delta(id, BookAction::Add, OrderSide::Sell, "101.00", "10.000", 2, 1),
            trade(id, "100.00", "80.000", AggressorSide::Seller, 2, "F02B-T1"),
            delta(id, BookAction::Update, OrderSide::Buy, "100.00", "20.000", 3, 3),
        ],
        None,
        true,
        true,
    )?;
    engine.run(None, None, None, true)?;
    engine.clear_data();
    let after_trade_1 = limit_order(&engine, id)
        .map(|order| order_snapshot(&order))
        .unwrap_or_else(|| json!({"error": "limit order not found after trade 1"}));

    // Chunk 2: second qualifying seller trade of 30.
    engine.add_data(
        vec![trade(
            id,
            "100.00",
            "30.000",
            AggressorSide::Seller,
            4,
            "F02B-T2",
        )],
        None,
        true,
        true,
    )?;
    engine.run(None, None, None, true)?;
    engine.clear_data();
    engine.end();

    let after_trade_2 = limit_order(&engine, id)
        .map(|order| order_snapshot(&order))
        .unwrap_or_else(|| json!({"error": "limit order not found after trade 2"}));
    let filled_1 = after_trade_1["filled_qty"].as_f64().unwrap_or(-1.0);
    let filled_2 = after_trade_2["filled_qty"].as_f64().unwrap_or(-1.0);
    let fill_step_2 = filled_2 - filled_1;

    Ok(json!({
        "fixture": "F02+depth-once",
        "setup": {"ahead": 100.0, "order_qty": 20.0, "trades": [80.0, 30.0], "depth_delta_after_trade": 20.0},
        "api": {
            "book_type": "L2_MBP",
            "deltas": "nautilus_model::data::OrderBookDelta (BookAction::Add/Update, aggregate flags=0)",
            "consumption_guard": "seed_trade_consumption skips when book.ts_last > trade.ts_event; adjust_queue_for_delta caps ahead instead of subtracting",
        },
        "after_trade_1": after_trade_1,
        "after_trade_2": after_trade_2,
        "filled_after_trade_1_qty": filled_1,
        "fill_step_2_qty": fill_step_2,
        "expected_fill_step_2_qty": 10.0,
        "depth_reduction_double_consumed": (fill_step_2 - 10.0).abs() >= 1e-9,
        "pass": (filled_1 - 0.0).abs() < 1e-9 && (fill_step_2 - 10.0).abs() < 1e-9,
    }))
}

// ---------------------------------------------------------------------------
// get_quantity_at_level direct public API check
// ---------------------------------------------------------------------------

fn run_exact_level_qty() -> anyhow::Result<Value> {
    let id = InstrumentId::from("ETHUSDT-PERP.BINANCE");
    let mut book = OrderBook::new(id, BookType::L2_MBP);
    book.add(
        BookOrder::new(
            OrderSide::Buy,
            Price::from("100.00"),
            Quantity::from("100.000"),
            1,
        ),
        0,
        1,
        ts(1),
    );
    let qty = book.get_quantity_at_level(Price::from("100.00"), OrderSide::Sell, 3);
    Ok(json!({
        "hook": "get_quantity_at_level",
        "api": "nautilus_model::orderbook::OrderBook::get_quantity_at_level(Price, OrderSide, u8) -> Quantity",
        "public": true,
        "input": {"side_in_book": "Buy", "price": 100.0, "size": 100.0, "queried_order_side": "Sell"},
        "observed_qty": qty.as_f64(),
        "expected_qty": 100.0,
        "pass": (qty.as_f64() - 100.0).abs() < 1e-9,
    }))
}

// ---------------------------------------------------------------------------
// F11: mark-based isolated liquidation
// ---------------------------------------------------------------------------

fn build_liq_engine(
    balance: &str,
    liquidation_enabled: bool,
    instrument: &InstrumentAny,
) -> anyhow::Result<BacktestEngine> {
    let mut engine = new_engine()?;
    engine.add_venue(venue_config(
        balance,
        BookType::L1_MBP,
        false,
        false,
        liquidation_enabled,
    ))?;
    engine.add_instrument(instrument)?;
    engine.add_strategy(OneShotMarket::new(instrument.id(), "1.000"))?;
    Ok(engine)
}

fn run_f11_mark() -> anyhow::Result<Value> {
    let instrument = instrument();
    let id = instrument.id();

    // Engine A: liquidation disabled. Observe the exact trigger inputs at mark 94.
    let mut observe = build_liq_engine("10 USDT", false, &instrument)?;
    observe.add_data(
        vec![
            quote(id, "99.99", "100.00", "10.000", "10.000", 1),
            mark(id, "100.00", 1),
        ],
        None,
        true,
        true,
    )?;
    observe.run(None, None, None, true)?;
    observe.clear_data();
    observe.add_data(
        vec![
            quote(id, "94.00", "94.10", "10.000", "10.000", 2),
            mark(id, "94.00", 2),
        ],
        None,
        true,
        true,
    )?;
    observe.run(None, None, None, true)?;
    observe.clear_data();
    observe.end();

    let (cash, maintenance) = account_state(&observe);
    let upnl_94 = upnl(&observe, id);
    let equity = cash + upnl_94;
    let open_observe = open_positions(&observe, id);
    let position = {
        let cache = observe.kernel().cache.borrow();
        cache
            .positions_open(None, Some(&id), None, None, None)
            .first()
            .map(|p| {
                json!({
                    "quantity": p.quantity.as_f64(),
                    "avg_px_open": p.avg_px_open,
                    "signed_qty": p.signed_qty,
                })
            })
            .unwrap_or(Value::Null)
    };

    // Engine B: liquidation enabled. Same tape, observe forced close.
    let mut forced = build_liq_engine("10 USDT", true, &instrument)?;
    forced.add_data(
        vec![
            quote(id, "99.99", "100.00", "10.000", "10.000", 1),
            mark(id, "100.00", 1),
        ],
        None,
        true,
        true,
    )?;
    forced.run(None, None, None, true)?;
    forced.clear_data();
    forced.add_data(
        vec![
            quote(id, "94.00", "94.10", "10.000", "10.000", 2),
            mark(id, "94.00", 2),
        ],
        None,
        true,
        true,
    )?;
    forced.run(None, None, None, true)?;
    forced.clear_data();
    forced.end();

    let liquidations = liquidation_orders(&forced, id);
    let liquidation_orders_json: Vec<Value> = liquidations.iter().map(order_snapshot).collect();
    let closed = closed_position(&forced, id);
    let (final_cash, final_maintenance) = account_state(&forced);
    let open_after = open_positions(&forced, id);

    Ok(json!({
        "fixture": "F11",
        "scenario": "MARK",
        "setup": {"isolated_cash": 10.0, "maintenance_rate": 0.05, "qty": 1.0, "entry": 100.0, "mark": 94.0},
        "api": {
            "venue_config": "SimulatedVenueConfig::builder().account_type(Margin).margin_model(MarginModelAny::Standard(StandardMarginModel)).liquidation_enabled(true).liquidation_trigger_ratio(1.0)",
            "engine_step": "BacktestEngine::run -> private fn run_venue_liquidations -> SimulatedExchange::process_liquidations -> OrderMatchingEngine::liquidate_open_positions",
            "symbol_visibility": "process_liquidations and liquidate_open_positions are pub but unreachable from BacktestEngine (venues and matching engines are private); only the config flags and engine step are public",
        },
        "observed_inputs": {
            "account_cash": cash,
            "quote_bid": 94.0,
            "unrealized_pnl_from_cache": upnl_94,
            "isolated_equity": equity,
            "maintenance_from_margin_account": maintenance,
            "maintenance_source": "PortfolioManager::update_balance_margin uses net_avg_px (entry 100.0), not the mark; StandardMarginModel => 100.0 * 0.05",
            "open_positions": open_observe,
            "position": position,
        },
        "oracle_expected": {"isolated_equity": 4.0, "maintenance": 4.7, "liquidation_triggered": true, "liquidation_cost": 0.09},
        "forced_close": {
            "liquidation_order_count": liquidations.len(),
            "liquidation_orders": liquidation_orders_json,
            "open_positions_after": open_after,
            "final_cash": final_cash,
            "final_maintenance": final_maintenance,
            "oracle_final_isolated_cash": 3.91,
            "final_cash_delta_vs_oracle": final_cash - 3.91,
            "final_cash_difference_reason": "oracle charges a 0.09 liquidation fee; Nautilus has no liquidation-fee field and the liquidation is a normal market order",
            "closed_position": closed.map(|p| json!({
                "quantity": p.quantity.as_f64(),
                "avg_px_open": p.avg_px_open,
                "avg_px_close": p.avg_px_close,
                "realized_pnl": p.realized_pnl.map(|m| m.as_f64()),
                "adjustments": adjustments(&p),
            })),
        },
        "verdicts": {
            "liquidation_triggered": open_after == 0 && liquidations.len() == 1,
            "isolated_equity_matches_oracle": (equity - 4.0).abs() < 1e-9,
            "maintenance_matches_oracle_mark_based": (maintenance - 4.7).abs() < 1e-9,
            "mark_is_trigger_source": false,
            "liquidation_cost_modeled": false,
            "final_cash_matches_oracle": ((final_cash - 3.91).abs() < 1e-9),
        },
        "pass": open_after == 0 && liquidations.len() == 1 && (equity - 4.0).abs() < 1e-9 && (maintenance - 4.7).abs() < 1e-9,
        "partial_note": "Trigger and equity match the oracle; maintenance does not. Nautilus computes maintenance from entry price and unrealized PnL from the quote bid, so the fixture's mark-based 4.7 is not reproduced. Native 5.0 still breaches at equity 4.0, so the forced close fires for a different reason than the fixture.",
    }))
}

fn run_f11_quote_only() -> anyhow::Result<Value> {
    let instrument = instrument();
    let id = instrument.id();
    let mut forced = build_liq_engine("10 USDT", true, &instrument)?;
    forced.add_data(
        vec![quote(id, "99.99", "100.00", "10.000", "10.000", 1)],
        None,
        true,
        true,
    )?;
    forced.run(None, None, None, true)?;
    forced.clear_data();
    // No MarkPriceUpdate at all: only a book/quote move to 94.
    forced.add_data(
        vec![quote(id, "94.00", "94.10", "10.000", "10.000", 2)],
        None,
        true,
        true,
    )?;
    forced.run(None, None, None, true)?;
    forced.clear_data();
    forced.end();

    let liquidations = liquidation_orders(&forced, id);
    let open_after = open_positions(&forced, id);
    let (final_cash, _) = account_state(&forced);

    Ok(json!({
        "fixture": "F11",
        "scenario": "QUOTE_IS_NOT_MARK",
        "setup": {"isolated_cash": 10.0, "qty": 1.0, "entry": 100.0, "quote": {"bid": 94.0, "ask": 94.1}, "mark_events": 0},
        "observed": {
            "liquidation_order_count": liquidations.len(),
            "open_positions_after": open_after,
            "final_cash": final_cash,
            "unrealized_pnl_source": "Cache::calculate_unrealized_pnl uses quote.bid_price for longs",
        },
        "oracle_expected": {"mark_trigger_inferred_from_quote": false, "mark_path_certificate": "missing", "liquidation_triggered": false},
        "verdict": if open_after == 0 { "FAIL_QUOTE_TRIGGERS_LIQUIDATION" } else { "PASS_QUOTE_DOES_NOT_TRIGGER" },
        "pass": open_after != 0,
        "finding": "Without any MarkPriceUpdate, a quote bid drop to 94 alone triggers the native forced close. F11 QUOTE_IS_NOT_MARK fails: the native trigger source is the quote, not a certified mark.",
    }))
}

// ---------------------------------------------------------------------------
// F12: funding debit crosses maintenance at unchanged mark
// ---------------------------------------------------------------------------

fn run_f12(boundary_offset: u64, funding_event_at: u64) -> anyhow::Result<Value> {
    let instrument = instrument();
    let id = instrument.id();

    // Engine A: observe funding + equity/maintenance without the forced close.
    let mut observe = build_liq_engine("5.1 USDT", false, &instrument)?;
    observe.add_data(
        vec![quote(id, "99.99", "100.00", "10.000", "10.000", 1)],
        None,
        true,
        true,
    )?;
    observe.run(None, None, None, true)?;
    observe.clear_data();
    observe.add_data(
        vec![
            quote(id, "100.00", "100.10", "10.000", "10.000", 2),
            mark(id, "100.00", 2),
            funding(id, "0.002", 480, boundary_offset, funding_event_at),
        ],
        None,
        true,
        true,
    )?;
    observe.run(None, None, None, true)?;
    observe.clear_data();
    observe.end();

    let (cash, maintenance) = account_state(&observe);
    let upnl_now = upnl(&observe, id);
    let equity = cash + upnl_now;
    let open_observe = open_positions(&observe, id);
    let funding_adjustments = {
        let cache = observe.kernel().cache.borrow();
        cache
            .positions_open(None, Some(&id), None, None, None)
            .first()
            .map(|position| adjustments(position))
            .unwrap_or_default()
    };

    // Engine B: same tape with liquidation enabled.
    let mut forced = build_liq_engine("5.1 USDT", true, &instrument)?;
    forced.add_data(
        vec![quote(id, "99.99", "100.00", "10.000", "10.000", 1)],
        None,
        true,
        true,
    )?;
    forced.run(None, None, None, true)?;
    forced.clear_data();
    forced.add_data(
        vec![
            quote(id, "100.00", "100.10", "10.000", "10.000", 2),
            mark(id, "100.00", 2),
            funding(id, "0.002", 480, boundary_offset, funding_event_at),
        ],
        None,
        true,
        true,
    )?;
    forced.run(None, None, None, true)?;
    forced.clear_data();
    forced.end();

    let liquidations = liquidation_orders(&forced, id);
    let closed = closed_position(&forced, id);
    let (final_cash, _) = account_state(&forced);
    let open_after = open_positions(&forced, id);

    let funding_pnl: f64 = funding_adjustments
        .iter()
        .filter_map(|a| a["pnl_change"].as_f64())
        .sum();

    Ok(json!({
        "fixture": "F12",
        "scenario": "BASE",
        "setup": {"isolated_cash": 5.1, "maintenance_rate": 0.05, "qty": 1.0, "entry": 100.0, "mark": 100.0, "funding_rate": 0.002},
        "timing": {"boundary_ns": 1_000_000_000 + boundary_offset, "funding_event_ns": 1_000_000_000 + funding_event_at, "boundary_is_exact": boundary_offset == funding_event_at},
        "observed_inputs": {
            "account_cash_after_funding": cash,
            "unrealized_pnl": upnl_now,
            "isolated_equity": equity,
            "maintenance_from_margin_account": maintenance,
            "open_positions": open_observe,
            "funding_adjustments": funding_adjustments,
            "funded_pnl": funding_pnl,
        },
        "oracle_expected": {"funding_cash": -0.2, "isolated_equity": 4.9, "maintenance": 5.0, "liquidation_triggered": true, "liquidation_cost": 0.1},
        "forced_close": {
            "liquidation_order_count": liquidations.len(),
            "open_positions_after": open_after,
            "final_cash": final_cash,
            "oracle_final_isolated_cash": 4.80,
            "final_cash_delta_vs_oracle": final_cash - 4.80,
            "final_cash_difference_reason": "oracle charges a 0.10 liquidation fee; Nautilus has no liquidation-fee field and the liquidation is a normal market order",
            "closed_position": closed.map(|p| json!({
                "quantity": p.quantity.as_f64(),
                "realized_pnl": p.realized_pnl.map(|m| m.as_f64()),
                "adjustments": adjustments(&p),
            })),
        },
        "verdicts": {
            "funding_cash_matches_oracle": (funding_pnl + 0.2).abs() < 1e-9,
            "isolated_equity_matches_oracle": (equity - 4.9).abs() < 1e-9,
            "maintenance_matches_oracle": (maintenance - 5.0).abs() < 1e-9,
            "liquidation_triggered": open_after == 0 && liquidations.len() == 1,
            "liquidation_cost_modeled": false,
            "final_cash_matches_oracle": ((final_cash - 4.80).abs() < 1e-9),
        },
        "pass": (funding_pnl + 0.2).abs() < 1e-9 && (equity - 4.9).abs() < 1e-9 && (maintenance - 5.0).abs() < 1e-9 && open_after == 0 && liquidations.len() == 1,
        "note": "F12 numeric paths match exactly because mark == entry. The native account is a single cross-margin balance per currency, so 'isolated' is not represented as an isolated sub-account; the match is for a single-position, single-currency setup only.",
    }))
}

fn run_f12_late() -> anyhow::Result<Value> {
    let instrument = instrument();
    let id = instrument.id();
    let mut engine = build_liq_engine("5.1 USDT", false, &instrument)?;
    engine.add_data(
        vec![quote(id, "99.99", "100.00", "10.000", "10.000", 1)],
        None,
        true,
        true,
    )?;
    engine.run(None, None, None, true)?;
    engine.clear_data();

    // Boundary at ts=2, but the funding update only arrives at ts=3.
    engine.add_data(
        vec![
            quote(id, "100.00", "100.10", "10.000", "10.000", 2),
            mark(id, "100.00", 2),
            funding(id, "0.002", 480, 2, 3),
        ],
        None,
        true,
        true,
    )?;
    let run_error = engine.run(None, None, None, true).err().map(|e| format!("{e:#}"));

    let (cash, maintenance) = account_state(&engine);
    let open = open_positions(&engine, id);
    let funding_adjustments = {
        let cache = engine.kernel().cache.borrow();
        cache
            .positions_open(None, Some(&id), None, None, None)
            .first()
            .map(|position| adjustments(position))
            .unwrap_or_default()
    };

    Ok(json!({
        "fixture": "F12",
        "scenario": "LATE_RESPONSE",
        "timing": {"boundary_ns": 1_000_000_002, "funding_event_ns": 1_000_000_003},
        "run_returned_error": run_error.is_some(),
        "run_error": run_error,
        "funding_applied": !funding_adjustments.is_empty(),
        "funding_adjustments": funding_adjustments,
        "account_cash": cash,
        "maintenance": maintenance,
        "open_positions": open,
        "liquidation_triggered": false,
        "expected": "deterministic rejection of a late funding boundary; no silent settlement and no look-ahead settlement price",
        "pass": run_error.is_some() && funding_adjustments.is_empty() && open == 1 && (cash - 5.1).abs() < 1e-9,
        "finding": "A funding update whose settlement boundary is already in the past aborts the run with 'Late funding boundary ... < replay timestamp' and applies no funding adjustment. The exact-boundary case (boundary == replay timestamp) settles once.",
    }))
}

// ---------------------------------------------------------------------------
// Native postings vs oracle
// ---------------------------------------------------------------------------

fn run_postings_check() -> anyhow::Result<Value> {
    // Native exposes no journal/posting API. Record exactly what exists.
    let instrument = instrument();
    let id = instrument.id();
    let mut engine = new_engine()?;
    engine.add_venue(venue_config("1000 USDT", BookType::L1_MBP, false, false, false))?;
    engine.add_instrument(&instrument)?;
    engine.add_strategy(OneShotMarket::new(id, "1.000"))?;
    engine.add_data(
        vec![quote(id, "99.99", "100.00", "10.000", "10.000", 1)],
        None,
        true,
        true,
    )?;
    engine.run(None, None, None, false)?;
    engine.end();
    let _result = engine.get_result();
    let (cash, maintenance) = account_state(&engine);
    Ok(json!({
        "hook": "native_account_postings",
        "journal_or_posting_api": false,
        "double_entry_ledger": false,
        "result_fields": ["trader_id", "machine_id", "instance_id", "iterations", "total_events", "total_orders", "total_positions", "summary", "stats_pnls", "stats_returns", "stats_general", "returns_series"],
        "available_output": {
            "account_balance_total": cash,
            "account_maintenance_margin": maintenance,
            "cache_orders": true,
            "cache_positions": true,
            "cache_fills_in_position_events": true,
        },
        "note": "BacktestResult has no accounts/orders/fills/postings. Account balance and position state are only readable from Cache; there is no double-entry journal to compare with the Python accounting oracle.",
        "pass": false,
    }))
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

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

fn main() -> anyhow::Result<()> {
    let started = Instant::now();

    let f02 = probe(run_f02_l1);
    let f02_depth = probe(run_f02_l2_depth_once);
    let exact_level = probe(run_exact_level_qty);
    let f11_mark = probe(run_f11_mark);
    let f11_quote = probe(run_f11_quote_only);
    let f12_base = probe(|| run_f12(2, 2));
    let f12_late = probe(run_f12_late);

    let p_f02 = f02["pass"].as_bool().unwrap_or(false);
    let p_f02_depth = f02_depth["pass"].as_bool().unwrap_or(false);
    let p_level_qty = exact_level["pass"].as_bool().unwrap_or(false);
    let p_f11_trigger = f11_mark["verdicts"]["liquidation_triggered"]
        .as_bool()
        .unwrap_or(false);
    let p_f11_maintenance = f11_mark["verdicts"]["maintenance_matches_oracle_mark_based"]
        .as_bool()
        .unwrap_or(false);
    let p_f11_quote = f11_quote["pass"].as_bool().unwrap_or(false);
    let p_f12 = f12_base["pass"].as_bool().unwrap_or(false);
    let p_f12_late = f12_late["pass"].as_bool().unwrap_or(false);

    let elapsed_ms = started.elapsed().as_millis() as u64;

    let output = json!({
        "spike": "qel-native-functional-capability-spike",
        "date": "2026-11-09",
        "scope": ["#3", "#4", "#10", "#11"],
        "pinned": {
            "repo": "https://github.com/nautechsystems/nautilus_trader",
            "tag": "v1.231.0",
            "commit": "27a8e54e7ac3c57d6cbf8891f0283dfbaee97317",
        },
        "hook_verdicts": {
            "l2_queue_ahead_snapshot": {
                "verdict": if p_f02 { "PASS_BEHAVIOR" } else { "FAIL" },
                "api": "SimulatedVenueConfig::builder().queue_position(true)",
                "exact_symbol": "fn snapshot_queue_position",
                "symbol_visibility": "private on OrderMatchingEngine; no public readback",
            },
            "l2_queue_decrement_on_trade": {
                "verdict": if p_f02 && p_f02_depth { "PASS_BEHAVIOR" } else { "FAIL" },
                "api": "queue_position(true) + TradeTick",
                "exact_symbol": "fn decrement_queue_on_trade",
                "symbol_visibility": "private",
            },
            "l2_liquidity_consumption": {
                "verdict": "PARTIAL",
                "api": "liquidity_consumption(true)",
                "exact_symbol": "fn apply_liquidity_consumption",
                "symbol_visibility": "private",
                "note": "exercised for the maker queue path; marketable-order budget not separately asserted",
            },
            "l2_exact_level_qty": {
                "verdict": if p_level_qty { "PASS" } else { "FAIL" },
                "api": "OrderBook::get_quantity_at_level(Price, OrderSide, u8) -> Quantity",
                "symbol_visibility": "public",
            },
            "liquidation_trigger_path": {
                "verdict": if p_f11_trigger && p_f12 { "PASS_BEHAVIOR" } else { "FAIL" },
                "api": "liquidation_enabled(true) + liquidation_trigger_ratio(1.0) + engine step",
                "exact_symbols": ["fn run_venue_liquidations", "SimulatedExchange::process_liquidations", "OrderMatchingEngine::liquidate_open_positions"],
                "symbol_visibility": "run_venue_liquidations private; the other two are pub but unreachable from BacktestEngine",
            },
            "liquidation_maintenance_mark_based": {
                "verdict": if p_f11_maintenance { "PASS" } else { "FAIL" },
                "observed": 5.0,
                "oracle_expected": 4.7,
                "reason": "maintenance is computed from net_avg_px (entry 100), not the mark 94",
            },
            "liquidation_quote_is_not_mark": {
                "verdict": if p_f11_quote { "PASS" } else { "FAIL" },
                "reason": "unrealized PnL uses quote.bid_price; a quote-only move to 94 triggers the forced close",
            },
            "funding_exact_boundary": {
                "verdict": if p_f12 { "PASS" } else { "FAIL" },
                "reason": "boundary == replay timestamp settles exactly once",
            },
            "funding_late_boundary": {
                "verdict": if p_f12_late { "PASS" } else { "FAIL" },
                "reason": "late boundary aborts the run and applies no funding adjustment",
            },
            "isolated_margin_account": {
                "verdict": "FAIL",
                "reason": "AccountType::Margin is one cross-margin balance per currency; no isolated sub-account API",
            },
            "liquidation_fee": {
                "verdict": "FAIL",
                "reason": "no liquidation-fee field; the forced close is a normal market order",
            },
            "native_account_postings": {
                "verdict": "FAIL_NOT_EXPOSED",
                "reason": "no journal or double-entry posting API; BacktestResult exposes aggregate stats only",
            },
        },
        "probes": {
            "f02_queue_l1": f02,
            "f02_depth_once_l2": f02_depth,
            "exact_level_qty": exact_level,
            "f11_mark": f11_mark,
            "f11_quote_is_not_mark": f11_quote,
            "f12_base_exact_boundary": f12_base,
            "f12_late_boundary_response": f12_late,
            "native_postings": run_postings_check().unwrap_or_else(|e| json!({"error": format!("{e:#}")})),
        },
        "resources": {
            "elapsed_ms": elapsed_ms,
            "peak_rss_kb": peak_rss_kb(),
        },
    });

    let text = serde_json::to_string_pretty(&output)?;
    match std::env::args().nth(1) {
        Some(path) => std::fs::write(path, text + "\n")?,
        None => println!("{text}"),
    }
    Ok(())
}
