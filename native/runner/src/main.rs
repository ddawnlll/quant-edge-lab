//! QEL native L1 taker runner: Nautilus replay driven by the typed policy DAG.

use std::{
    collections::{BTreeMap, HashMap, VecDeque},
    fmt::Debug,
    fs::{self, File},
    io::{BufRead, BufReader, Write},
    sync::{Arc, Mutex},
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
    events::{OrderFilled, PositionClosed, PositionEvent, PositionOpened},
    identifiers::{InstrumentId, PositionId, StrategyId, Symbol, TradeId, Venue},
    instruments::{CryptoPerpetual, Instrument, InstrumentAny},
    types::{Currency, Money, Price, Quantity},
};
use nautilus_trading::{Strategy, StrategyConfig, StrategyCore, nautilus_strategy};
use qel_policy::{parse_strategy, Engine as PolicyEngine, StepOutput};
use rust_decimal::Decimal;
use serde_json::{json, Value};

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

/// Cumulative run counters shared with the main thread.
///
/// These are tracked by the strategy itself instead of being read back from the
/// Nautilus cache because closed cache state is purged during bounded replay.
#[derive(Default)]
struct RunStats {
    orders_submitted: usize,
    positions_opened: usize,
}

/// Bounded funding-epoch ledger.
///
/// Open position epochs are retained. Closed epochs are released after the
/// configured retention window once their economic postings are already in the
/// durable tape. The settlement identity embedded in the tape is the audit
/// history; this ledger only exists to label live funding callbacks.
struct EpochState {
    counter: u64,
    current: HashMap<PositionId, (u64, u64)>,
    closed: VecDeque<(PositionId, u64, u64)>,
    purged: usize,
}

impl EpochState {
    fn new() -> Self {
        Self {
            counter: 0,
            current: HashMap::new(),
            closed: VecDeque::new(),
            purged: 0,
        }
    }

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

    fn purge_closed_before(&mut self, ts_now: u64, retention_ns: u64) -> usize {
        let mut purged = 0usize;
        while let Some(&(position_id, epoch, ts_closed)) = self.closed.front() {
            if ts_closed.saturating_add(retention_ns) > ts_now {
                break;
            }
            self.closed.pop_front();
            let remove = self
                .current
                .get(&position_id)
                .is_some_and(|(current_epoch, _)| *current_epoch == epoch);
            if remove && self.current.remove(&position_id).is_some() {
                purged += 1;
            }
        }
        self.purged += purged;
        purged
    }

    fn len(&self) -> usize {
        // Active epochs plus closed epochs still inside the retention window.
        // Closed entries are released by `purge_closed_before`, so this is the
        // real bounded active-history footprint rather than just the single
        // currently open position.
        self.current.len() + self.closed.len()
    }
}

struct Taker {
    core: StrategyCore,
    instrument_id: InstrumentId,
    policy: PolicyEngine,
    notional: f64,
    bar_ns: u64,
    bucket: u64,
    last_mid: Option<f64>,
    last_trade: Option<f64>,
    tape: Arc<Mutex<Vec<Value>>>,
    stats: Arc<Mutex<RunStats>>,
    epochs: Arc<Mutex<EpochState>>,
    position_side: i8,
    position_open_ns: u64,
    max_holding_ns: u64,
    decisions: usize,
}

impl Taker {
    fn new(
        instrument_id: InstrumentId,
        policy: PolicyEngine,
        notional: f64,
        bar_ns: u64,
        max_holding_ns: u64,
        stats: Arc<Mutex<RunStats>>,
        epochs: Arc<Mutex<EpochState>>,
    ) -> Self {
        let config = StrategyConfig {
            strategy_id: Some(StrategyId::from("TAKER-NATIVE-001")),
            order_id_tag: Some("001".to_string()),
            ..Default::default()
        };
        Self {
            core: StrategyCore::new(config),
            instrument_id,
            policy,
            notional,
            bar_ns,
            bucket: 0,
            last_mid: None,
            last_trade: None,
            tape: Arc::new(Mutex::new(vec![json!({"type": "SET_WALLET", "cash": "1000"})])),
            stats,
            epochs,
            position_side: 0,
            position_open_ns: 0,
            max_holding_ns,
            decisions: 0,
        }
    }

    fn has_position(&self) -> bool {
        let sid = StrategyId::from("TAKER-NATIVE-001");
        !self.cache().positions_open(None, Some(&self.instrument_id), Some(&sid), None, None).is_empty()
    }

    fn decide(&mut self, price: f64, now_ns: u64) -> Result<()> {
        let mut inputs = HashMap::new();
        inputs.insert("MID_CLOSE".to_string(), Some(price));
        inputs.insert("TRADE_CLOSE".to_string(), self.last_trade);
        let out: StepOutput = self.policy.step(&inputs, now_ns)?;
        if !out.decision_enabled {
            return Ok(());
        }
        self.decisions += 1;
        if self.has_position() {
            let exit = match self.position_side {
                1 => out.long_exit,
                -1 => out.short_exit,
                _ => false,
            } || (self.max_holding_ns > 0 && now_ns.saturating_sub(self.position_open_ns) >= self.max_holding_ns);
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
        let sid = StrategyId::from("TAKER-NATIVE-001");
        let positions: Vec<(_, _, _)> = self
            .cache()
            .positions_open(None, Some(&self.instrument_id), Some(&sid), None, None)
            .iter()
            .map(|p| (p.id, p.quantity, p.side))
            .collect();
        for (position_id, quantity, side) in positions {
            let closing = if side == nautilus_model::enums::PositionSide::Long {
                OrderSide::Sell
            } else {
                OrderSide::Buy
            };
            let order = self.order().market(
                self.instrument_id,
                closing,
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
            self.submit_order(order, Some(position_id), None, None)?;
        }
        Ok(())
    }
}

nautilus_strategy!(Taker, {
    fn on_order_filled(&mut self, event: &OrderFilled) {
        if event.instrument_id != self.instrument_id {
            return;
        }
        let side = match event.order_side {
            OrderSide::Buy => "BUY",
            _ => "SELL",
        };
        let fee = event.commission.map(|m| m.as_f64()).unwrap_or(0.0);
        self.tape.lock().unwrap().push(json!({
            "type": "FILL",
            "ts_ns": event.ts_event.as_u64().to_string(),
            "side": side,
            "qty": trim(event.last_qty.as_f64(), 8),
            "price": trim(event.last_px.as_f64(), 8),
            "fee": trim(fee, 8),
        }));
    }
    fn on_position_opened(&mut self, event: PositionOpened) {
        if event.instrument_id == self.instrument_id {
            self.stats.lock().unwrap().positions_opened += 1;
            self.position_side = if event.side == nautilus_model::enums::PositionSide::Long { 1 } else { -1 };
            self.position_open_ns = event.ts_event.as_u64();
        }
    }
    fn on_position_closed(&mut self, event: PositionClosed) {
        if event.instrument_id == self.instrument_id {
            self.position_side = 0;
            self.position_open_ns = 0;
        }
    }
});

impl Debug for Taker {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Taker").finish()
    }
}

impl DataActor for Taker {
    fn on_start(&mut self) -> Result<()> {
        self.subscribe_quotes(self.instrument_id, None, None);
        self.subscribe_trades(self.instrument_id, None, None);

        // The pinned Nautilus build intentionally short-circuits
        // `PositionEvent::PositionAdjusted` before `Strategy::on_position_event`
        // (`trading/src/strategy/mod.rs`). Subscribe to the concrete position
        // event topic so funding adjustments enter the tape at the exact point
        // the native exchange settles them, in native causal order.
        //
        // The settlement identity is deterministic across identical replays:
        // the native settlement timestamp (`ts_event`) plus the position id and
        // the strategy position epoch. Nautilus' own adjustment `event_id` is a
        // random UUID4 and is deliberately excluded because it would break
        // semantic reproducibility of the tape.
        let topic = get_event_position_topic(StrategyId::from("TAKER-NATIVE-001"));
        let tape = self.tape.clone();
        let epochs = self.epochs.clone();
        let position_handler = TypedHandler::from(move |event: &PositionEvent| match event {
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
                let settlement_id = format!(
                    "FUND:{}:{}:{}",
                    adjusted.ts_event.as_u64(),
                    adjusted.position_id,
                    epoch
                );
                tape.lock().unwrap().push(json!({
                    "type": "FUNDING",
                    "amount": money.as_decimal().to_string(),
                    "currency": money.currency.code.to_string(),
                    "epoch": epoch.to_string(),
                    "settlement_id": settlement_id,
                    "position_id": adjusted.position_id.to_string(),
                    "account_id": adjusted.account_id.to_string(),
                    "ts_ns": adjusted.ts_event.as_u64().to_string(),
                }));
            }
            _ => {}
        });
        msgbus::subscribe_position_events(topic.into(), position_handler, None);
        Ok(())
    }

    fn on_quote(&mut self, quote: &QuoteTick) -> Result<()> {
        let mid = (quote.bid_price.as_f64() + quote.ask_price.as_f64()) / 2.0;
        let ts = quote.ts_event.as_u64();
        let bucket = ts / self.bar_ns;
        if self.bucket == 0 {
            self.bucket = bucket;
        } else if bucket != self.bucket {
            self.bucket = bucket;
            if let Some(prev) = self.last_mid {
                self.decide(prev, ts)?;
            }
        }
        self.last_mid = Some(mid);
        Ok(())
    }

    fn on_trade(&mut self, trade: &TradeTick) -> Result<()> {
        self.last_trade = Some(trade.price.as_f64());
        Ok(())
    }
}

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
    is_funding: bool,
}

/// Parse one complete NDJSON record. Callers must pass a whole line so a record
/// can never be split across chunk boundaries.
fn parse_event_line(line: &str, id: InstrumentId) -> Result<ParsedEvent> {
    let v: Value = serde_json::from_str(line).context("ndjson record")?;
    let ts = v["ts_ns"].as_str().unwrap_or("0").parse::<u64>()?;
    let kind = v["kind"].as_str().unwrap_or("");
    let mut mid = None;
    let data = match kind {
        "Quote" => {
            let bid: f64 = v["bid"].as_str().unwrap_or("0").parse()?;
            let ask: f64 = v["ask"].as_str().unwrap_or("0").parse()?;
            mid = Some((bid + ask) / 2.0);
            Some(Data::Quote(QuoteTick::new(
                id,
                round_px(v["bid"].as_str().unwrap_or("0"))?,
                round_px(v["ask"].as_str().unwrap_or("0"))?,
                round_qty(v["bid_qty"].as_str().unwrap_or("0"))?,
                round_qty(v["ask_qty"].as_str().unwrap_or("0"))?,
                UnixNanos::from(ts),
                UnixNanos::from(ts),
            )))
        }
        "Trade" => {
            let side = if v["aggressor"].as_str() == Some("SELL") { AggressorSide::Seller } else { AggressorSide::Buyer };
            let tid = TradeId::from(v["trade_id"].as_str().unwrap_or("T-0"));
            Some(Data::Trade(TradeTick::new(
                id,
                round_px(v["price"].as_str().unwrap_or("0"))?,
                round_qty(v["qty"].as_str().unwrap_or("0"))?,
                side,
                tid,
                UnixNanos::from(ts),
                UnixNanos::from(ts),
            )))
        }
        "Mark" => Some(Data::MarkPriceUpdate(MarkPriceUpdate::new(
            id,
            round_px(v["price"].as_str().unwrap_or("0"))?,
            UnixNanos::from(ts),
            UnixNanos::from(ts),
        ))),
        "Funding" => {
            let rate: Decimal = v["rate"].as_str().unwrap_or("0").parse()?;
            Some(Data::FundingRateUpdate(FundingRateUpdate::new(
                id,
                rate,
                Some(480),
                None,
                UnixNanos::from(ts),
                UnixNanos::from(ts),
            )))
        }
        _ => None,
    };
    Ok(ParsedEvent {
        ts,
        mid,
        data,
        is_funding: kind == "Funding",
    })
}

/// Insert pending funding-rate updates whose economic timestamp has been
/// reached. Updates that arrive late are applied at the earliest point the
/// one-pass reader can observe; economic quote/trade/mark events are never
/// reordered.
fn drain_fundings(
    pending: &mut BTreeMap<u64, Vec<(Data, usize)>>,
    upto: u64,
    batch: &mut Vec<Data>,
    batch_bytes: &mut usize,
) {
    while let Some((&ts, _)) = pending.first_key_value() {
        if ts > upto {
            break;
        }
        if let Some((_, items)) = pending.pop_first() {
            for (data, bytes) in items {
                batch.push(data);
                *batch_bytes += bytes;
            }
        }
    }
}

/// Collect funding-rate updates up front so they can be injected at their
/// economic timestamp even when the source file delivers them late (for example
/// a historical backfill at the end of a capture). Only funding records are
/// retained, so memory stays bounded by the funding count rather than the
/// event count.
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
            pending
                .entry(event.ts)
                .or_default()
                .push((data, n));
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

struct ResourceLimit {
    reason: &'static str,
    limit: u64,
    observed: u64,
}

struct Replay {
    max_active_orders: usize,
    max_epochs: usize,
    purge_buffer_secs: u64,
    epoch_retention_ns: u64,
    no_purge: bool,
    purged_orders: usize,
    purged_positions: usize,
    purged_epochs: usize,
    batches: usize,
    events_flushed: usize,
    samples: Vec<Value>,
    sample_file: Option<File>,
}

impl Replay {
    #[allow(clippy::too_many_arguments)]
    fn flush(
        &mut self,
        engine: &mut BacktestEngine,
        batch: &mut Vec<Data>,
        id: &InstrumentId,
        sid: &StrategyId,
        ts_now: u64,
        epochs: &Arc<Mutex<EpochState>>,
    ) -> Result<Option<ResourceLimit>> {
        if batch.is_empty() {
            return Ok(None);
        }
        self.batches += 1;
        let data = std::mem::take(batch);
        self.events_flushed += data.len();
        engine.add_data(data, None, true, true)?;
        engine.run(None, None, None, true)?;
        engine.clear_data();

        let (active_orders, active_positions) = {
            let cache = engine.kernel().cache.borrow();
            (
                cache.orders_open_count(None, Some(id), Some(sid), None, None),
                cache.positions_open_count(None, Some(id), Some(sid), None, None),
            )
        };
        let retained_epochs = epochs.lock().unwrap().len();

        let mut limit = None;
        if self.max_active_orders > 0 && active_orders > self.max_active_orders {
            limit = Some(ResourceLimit {
                reason: "MAX_ACTIVE_ORDERS",
                limit: self.max_active_orders as u64,
                observed: active_orders as u64,
            });
        } else if self.max_epochs > 0 && retained_epochs > self.max_epochs {
            limit = Some(ResourceLimit {
                reason: "MAX_EPOCHS",
                limit: self.max_epochs as u64,
                observed: retained_epochs as u64,
            });
        }

        if limit.is_none() && !self.no_purge {
            let (before_orders, before_positions) = {
                let cache = engine.kernel().cache.borrow();
                (
                    cache.orders_closed_count(None, Some(id), Some(sid), None, None),
                    cache.positions_closed_count(None, Some(id), Some(sid), None, None),
                )
            };
            {
                let mut cache = engine.kernel().cache.borrow_mut();
                cache.purge_closed_orders(UnixNanos::from(ts_now), self.purge_buffer_secs);
                cache.purge_closed_positions(UnixNanos::from(ts_now), self.purge_buffer_secs);
            }
            let (after_orders, after_positions) = {
                let cache = engine.kernel().cache.borrow();
                (
                    cache.orders_closed_count(None, Some(id), Some(sid), None, None),
                    cache.positions_closed_count(None, Some(id), Some(sid), None, None),
                )
            };
            self.purged_orders += before_orders.saturating_sub(after_orders);
            self.purged_positions += before_positions.saturating_sub(after_positions);
            self.purged_epochs += epochs
                .lock()
                .unwrap()
                .purge_closed_before(ts_now, self.epoch_retention_ns);
        }

        let sample = json!({
            "batch": self.batches,
            "events": self.events_flushed,
            "ts_ns": ts_now.to_string(),
            "rss_kb": current_rss_kb(),
            "peak_rss_kb": peak_rss_kb(),
            "active_orders": active_orders,
            "active_positions": active_positions,
            "epochs_retained": retained_epochs,
            "purged_orders": self.purged_orders,
            "purged_positions": self.purged_positions,
            "purged_epochs": self.purged_epochs,
        });
        if let Some(file) = self.sample_file.as_mut() {
            writeln!(file, "{}", serde_json::to_string(&sample)?)?;
        }
        self.samples.push(sample);
        Ok(limit)
    }
}

fn main() -> Result<()> {
    let started = std::time::Instant::now();
    let args: Vec<String> = std::env::args().collect();
    let mut events = None;
    let mut strategy_path = None;
    let mut out_path = None;
    let mut notional = 100.0f64;
    let mut chunk = 0usize;
    let mut chunk_bytes = 0usize;
    let mut max_active_orders = 10_000usize;
    let mut max_epochs = 1_000_000usize;
    let mut max_pending_fundings = 100_000usize;
    let mut purge_buffer_secs = 0u64;
    let mut epoch_retention_ns = 86_400_000_000_000u64;
    let mut rss_samples_path: Option<String> = None;
    let mut no_purge = false;
    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--events" => { events = args.get(i + 1).cloned(); i += 2; }
            "--strategy" => { strategy_path = args.get(i + 1).cloned(); i += 2; }
            "--out" => { out_path = args.get(i + 1).cloned(); i += 2; }
            "--notional" => { notional = args.get(i + 1).ok_or_else(|| anyhow!("notional"))?.parse()?; i += 2; }
            "--chunk" => { chunk = args.get(i + 1).ok_or_else(|| anyhow!("chunk"))?.parse()?; i += 2; }
            "--chunk-bytes" => { chunk_bytes = args.get(i + 1).ok_or_else(|| anyhow!("chunk-bytes"))?.parse()?; i += 2; }
            "--max-active-orders" => { max_active_orders = args.get(i + 1).ok_or_else(|| anyhow!("max-active-orders"))?.parse()?; i += 2; }
            "--max-epochs" => { max_epochs = args.get(i + 1).ok_or_else(|| anyhow!("max-epochs"))?.parse()?; i += 2; }
            "--max-pending-fundings" => { max_pending_fundings = args.get(i + 1).ok_or_else(|| anyhow!("max-pending-fundings"))?.parse()?; i += 2; }
            "--purge-buffer-secs" => { purge_buffer_secs = args.get(i + 1).ok_or_else(|| anyhow!("purge-buffer-secs"))?.parse()?; i += 2; }
            "--epoch-retention-ns" => { epoch_retention_ns = args.get(i + 1).ok_or_else(|| anyhow!("epoch-retention-ns"))?.parse()?; i += 2; }
            "--rss-samples" => { rss_samples_path = args.get(i + 1).cloned(); i += 2; }
            "--no-purge" => { no_purge = true; i += 1; }
            other => bail!("unknown argument {other}"),
        }
    }
    let events_path = events.context("--events required")?;
    let strategy_text = fs::read_to_string(strategy_path.context("--strategy required")?)?;
    let strategy_json: Value = serde_json::from_str(&strategy_text)?;
    let parsed_strategy = parse_strategy(&strategy_json)?;
    let bar_ns = strategy_json["decision_clock"]["interval_ns"].as_str().unwrap_or("1000000000").parse::<u64>()?;
    let max_holding_ns = strategy_json["exit_policy"]["max_holding_ns"].as_str().unwrap_or("0").parse::<u64>()?;
    let instrument = instrument();
    let id = instrument.id();
    let sid = StrategyId::from("TAKER-NATIVE-001");
    let streaming = chunk > 0 || chunk_bytes > 0;

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
    let epochs = Arc::new(Mutex::new(EpochState::new()));
    let taker = Taker::new(
        id,
        PolicyEngine::new(parsed_strategy),
        notional,
        bar_ns,
        max_holding_ns,
        stats.clone(),
        epochs.clone(),
    );
    let tape_handle = taker.tape.clone();
    engine.add_strategy(taker)?;

    let sample_file = match &rss_samples_path {
        Some(path) => Some(File::create(path).context("rss samples file")?),
        None => None,
    };
    let mut replay = Replay {
        max_active_orders,
        max_epochs,
        purge_buffer_secs,
        epoch_retention_ns,
        no_purge,
        purged_orders: 0,
        purged_positions: 0,
        purged_epochs: 0,
        batches: 0,
        events_flushed: 0,
        samples: Vec::new(),
        sample_file,
    };

    let file = File::open(&events_path).context("events file")?;
    let mut reader = BufReader::with_capacity(1 << 20, file);
    let mut line = String::with_capacity(1024);
    let mut batch: Vec<Data> = Vec::new();
    let mut batch_bytes = 0usize;
    let mut batch_max_ts = 0u64;
    let mut parsed_events = 0usize;
    let mut final_mid: Option<f64> = None;
    let mut last_ts = 0u64;
    let mut pending_flush_ts: Option<u64> = None;
    let mut pending_fundings = collect_fundings(&events_path, id, max_pending_fundings)?;
    let mut limit: Option<ResourceLimit> = None;

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
        let Some(data) = event.data else {
            continue;
        };
        parsed_events += 1;
        last_ts = last_ts.max(event.ts);
        if event.is_funding {
            // Already collected by the funding pre-pass and injected at the
            // correct economic timestamp.
            continue;
        }
        if streaming {
            if let Some(flush_ts) = pending_flush_ts {
                // Keep every same-timestamp group inside one batch. The record
                // in hand starts a new timestamp, so publish the complete group
                // before adding it.
                if event.ts != flush_ts {
                    limit = replay.flush(
                        &mut engine,
                        &mut batch,
                        &id,
                        &sid,
                        batch_max_ts,
                        &epochs,
                    )?;
                    if limit.is_some() {
                        break;
                    }
                    pending_flush_ts = None;
                    batch_bytes = 0;
                    batch_max_ts = 0;
                }
            }
        }
        drain_fundings(
            &mut pending_fundings,
            event.ts,
            &mut batch,
            &mut batch_bytes,
        );
        batch.push(data);
        batch_bytes += n;
        batch_max_ts = batch_max_ts.max(event.ts);
        if streaming && pending_flush_ts.is_none() {
            let reached = if chunk_bytes > 0 {
                batch_bytes >= chunk_bytes
            } else {
                batch.len() >= chunk.max(1)
            };
            if reached {
                pending_flush_ts = Some(event.ts);
            }
        }
    }

    if parsed_events == 0 {
        bail!("no native events");
    }
    if streaming {
        drain_fundings(&mut pending_fundings, u64::MAX, &mut batch, &mut batch_bytes);
        if limit.is_none() && !batch.is_empty() {
            limit = replay.flush(
                &mut engine,
                &mut batch,
                &id,
                &sid,
                batch_max_ts,
                &epochs,
            )?;
        }
        if limit.is_none() {
            engine.end();
        }
    } else {
        drain_fundings(&mut pending_fundings, u64::MAX, &mut batch, &mut batch_bytes);
        engine.add_data(std::mem::take(&mut batch), None, true, true)?;
        engine.run(None, None, None, false)?;
    }

    let result = engine.get_result();
    let venue = Venue::from("BINANCE");
    let usdt = Currency::from("USDT");
    let mut tape_values = tape_handle.lock().unwrap().clone();
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
        }
        // Use the native mark used for account valuation rather than the last
        // quote midpoint so the independent mark-to-market identity is directly
        // comparable with the native account snapshot.
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
                "mark": final_mark.clone(),
            }),
            final_mark,
            native_mark_ts,
            cache.orders_open_count(None, Some(&id), Some(&sid), None, None),
            cache.positions_open_count(None, Some(&id), Some(&sid), None, None),
        )
    };
    let complete = limit.is_none();
    if complete {
        let max_ts = tape_values
            .iter()
            .filter_map(|value| value.get("ts_ns").and_then(|ts| ts.as_str()))
            .filter_map(|ts| ts.parse::<u64>().ok())
            .max()
            .unwrap_or(0);
        let mark_ts = native_mark_ts.max(max_ts);
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
    }
    let mut funding_count = 0usize;
    let mut funding_total = Decimal::ZERO;
    for value in &tape_values {
        if value.get("type").and_then(|kind| kind.as_str()) != Some("FUNDING") {
            continue;
        }
        funding_count += 1;
        if let Some(amount) = value.get("amount").and_then(|amount| amount.as_str()) {
            funding_total += amount.parse::<Decimal>().unwrap_or(Decimal::ZERO);
        }
    }
    let stats = stats.lock().unwrap();
    let mode = if chunk_bytes > 0 {
        "byte_bounded"
    } else if chunk > 0 {
        "event_count_bounded"
    } else {
        "materialized_reference"
    };
    let status = if complete { "PASS" } else { "RESOURCE_LIMIT" };
    let mut output = json!({
        "status": status,
        "tape": tape_values,
        "profile": "TAKER_L1_FUNDING_STREAMING",
        "events": parsed_events,
        "chunk_events": chunk,
        "chunk_bytes": chunk_bytes,
        "streaming": streaming,
        "iterations": result.iterations,
        "total_orders": stats.orders_submitted,
        "total_positions": stats.positions_opened,
        "engine_total_orders": result.total_orders,
        "engine_total_positions": result.total_positions,
        "pnl_total": result.summary.get("PnL (total)").cloned(),
        "funding": {
            "count": funding_count,
            "total": funding_total.to_string(),
            "source": "PositionAdjusted(Funding) event callback",
            "amount_currency": "USDT",
            "amount_exponent": 8,
        },
        "native_account": native_account,
        "events_path": events_path,
        "elapsed_ms": started.elapsed().as_millis() as u64,
        "peak_rss_kb": peak_rss_kb(),
        "resource": {
            "mode": mode,
            "batches": replay.batches,
            "max_active_orders": replay.max_active_orders,
            "max_epochs": replay.max_epochs,
            "purge_buffer_secs": replay.purge_buffer_secs,
            "epoch_retention_ns": replay.epoch_retention_ns,
            "purge_enabled": !replay.no_purge,
            "purged_closed_orders": replay.purged_orders,
            "purged_closed_positions": replay.purged_positions,
            "purged_epochs": replay.purged_epochs,
            "epochs_retained": epochs.lock().unwrap().len(),
            "active_orders_end": active_orders_end,
            "active_positions_end": active_positions_end,
            "events_processed": parsed_events,
            "events_replayed": replay.events_flushed,
            "ts_last_ns": last_ts.to_string(),
            "rss_samples_path": rss_samples_path,
            "rss_samples": replay.samples,
        },
    });
    if let Some(limit) = &limit {
        output["limit"] = json!({
            "reason": limit.reason,
            "limit": limit.limit,
            "observed": limit.observed,
        });
        output["partial_tape"] = json!(true);
    }
    if let Some(path) = out_path {
        fs::write(path, serde_json::to_string_pretty(&output)? + "\n")?;
    } else {
        println!("{}", serde_json::to_string_pretty(&output)?);
    }
    if let Some(limit) = &limit {
        eprintln!(
            "RESOURCE_LIMIT reason={} limit={} observed={} events_processed={}",
            limit.reason, limit.limit, limit.observed, parsed_events
        );
    }
    Ok(())
}
