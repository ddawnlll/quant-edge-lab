//! QEL native L1 taker runner: Nautilus replay driven by the typed policy DAG.

use std::{collections::HashMap, fmt::Debug, fs, sync::{Arc, Mutex}};

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
    position_side: i8,
    position_open_ns: u64,
    max_holding_ns: u64,
    decisions: usize,
}

impl Taker {
    fn new(instrument_id: InstrumentId, policy: PolicyEngine, notional: f64, bar_ns: u64, max_holding_ns: u64) -> Self {
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
        let epochs: Arc<Mutex<HashMap<PositionId, u64>>> = Arc::new(Mutex::new(HashMap::new()));
        let epoch_counter = Arc::new(Mutex::new(0u64));
        let position_handler = TypedHandler::from(move |event: &PositionEvent| match event {
            PositionEvent::PositionOpened(opened) => {
                let mut counter = epoch_counter.lock().unwrap();
                *counter += 1;
                epochs.lock().unwrap().insert(opened.position_id, *counter);
            }
            PositionEvent::PositionAdjusted(adjusted)
                if adjusted.adjustment_type == PositionAdjustmentType::Funding =>
            {
                let Some(money) = adjusted.pnl_change else {
                    return;
                };
                let epoch = epochs
                    .lock()
                    .unwrap()
                    .get(&adjusted.position_id)
                    .copied()
                    .unwrap_or_else(|| (*epoch_counter.lock().unwrap()).max(1));
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

fn last_mid(path: &str) -> Result<Option<f64>> {
    let text = fs::read_to_string(path)?;
    let mut last = None;
    for line in text.lines() {
        let v: Value = serde_json::from_str(line)?;
        if v["kind"].as_str() == Some("Quote") {
            let bid: f64 = v["bid"].as_str().unwrap_or("0").parse()?;
            let ask: f64 = v["ask"].as_str().unwrap_or("0").parse()?;
            last = Some((bid + ask) / 2.0);
        }
    }
    Ok(last)
}

fn read_events(path: &str, id: InstrumentId) -> Result<Vec<Data>> {
    let text = fs::read_to_string(path).context("events file")?;
    let mut out = Vec::new();
    for line in text.lines() {
        let v: Value = serde_json::from_str(line)?;
        let ts = v["ts_ns"].as_str().unwrap_or("0").parse::<u64>()?;
        let kind = v["kind"].as_str().unwrap_or("");
        match kind {
            "Quote" => out.push(Data::Quote(QuoteTick::new(
                id,
                round_px(v["bid"].as_str().unwrap_or("0"))?,
                round_px(v["ask"].as_str().unwrap_or("0"))?,
                round_qty(v["bid_qty"].as_str().unwrap_or("0"))?,
                round_qty(v["ask_qty"].as_str().unwrap_or("0"))?,
                UnixNanos::from(ts),
                UnixNanos::from(ts),
            ))),
            "Trade" => {
                let side = if v["aggressor"].as_str() == Some("SELL") { AggressorSide::Seller } else { AggressorSide::Buyer };
                let tid = TradeId::from(v["trade_id"].as_str().unwrap_or("T-0"));
                out.push(Data::Trade(TradeTick::new(
                    id,
                    round_px(v["price"].as_str().unwrap_or("0"))?,
                    round_qty(v["qty"].as_str().unwrap_or("0"))?,
                    side,
                    tid,
                    UnixNanos::from(ts),
                    UnixNanos::from(ts),
                )));
            }
            "Mark" => out.push(Data::MarkPriceUpdate(MarkPriceUpdate::new(
                id,
                round_px(v["price"].as_str().unwrap_or("0"))?,
                UnixNanos::from(ts),
                UnixNanos::from(ts),
            ))),
            "Funding" => {
                let rate: Decimal = v["rate"].as_str().unwrap_or("0").parse()?;
                out.push(Data::FundingRateUpdate(FundingRateUpdate::new(
                    id,
                    rate,
                    Some(480),
                    None,
                    UnixNanos::from(ts),
                    UnixNanos::from(ts),
                )));
            }
            _ => {}
        }
    }
    Ok(out)
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

fn main() -> Result<()> {
    let started = std::time::Instant::now();
    let args: Vec<String> = std::env::args().collect();
    let mut events = None;
    let mut strategy_path = None;
    let mut out_path = None;
    let mut notional = 100.0f64;
    let mut chunk = 0usize;
    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--events" => { events = args.get(i + 1).cloned(); i += 2; }
            "--strategy" => { strategy_path = args.get(i + 1).cloned(); i += 2; }
            "--out" => { out_path = args.get(i + 1).cloned(); i += 2; }
            "--notional" => { notional = args.get(i + 1).ok_or_else(|| anyhow!("notional"))?.parse()?; i += 2; }
            "--chunk" => { chunk = args.get(i + 1).ok_or_else(|| anyhow!("chunk"))?.parse()?; i += 2; }
            other => bail!("unknown argument {other}"),
        }
    }
    let events_path = events.context("--events required")?;
    let strategy_text = fs::read_to_string(strategy_path.context("--strategy required")?)?;
    let strategy_json: Value = serde_json::from_str(&strategy_text)?;
    let parsed = parse_strategy(&strategy_json)?;
    let bar_ns = strategy_json["decision_clock"]["interval_ns"].as_str().unwrap_or("1000000000").parse::<u64>()?;
    let max_holding_ns = strategy_json["exit_policy"]["max_holding_ns"].as_str().unwrap_or("0").parse::<u64>()?;
    let instrument = instrument();
    let id = instrument.id();
    let data = read_events(&events_path, id)?;
    let final_mid = last_mid(&events_path)?;
    if data.is_empty() {
        bail!("no native events");
    }

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
    let taker = Taker::new(id, PolicyEngine::new(parsed), notional, bar_ns, max_holding_ns);
    let tape_handle = taker.tape.clone();
    engine.add_strategy(taker)?;
    let count = data.len();
    if chunk > 0 {
        for batch in data.chunks(chunk) {
            engine.add_data(batch.to_vec(), None, true, true)?;
            engine.run(None, None, None, true)?;
            engine.clear_data();
        }
        engine.end();
    } else {
        engine.add_data(data, None, true, true)?;
        engine.run(None, None, None, false)?;
    }
    let result = engine.get_result();
    let venue = Venue::from("BINANCE");
    let usdt = Currency::from("USDT");
    let mut tape_values = tape_handle.lock().unwrap().clone();
    let (native_account, final_mark, native_mark_ts) = {
        let cache = engine.kernel().cache.borrow();
        let sid = StrategyId::from("TAKER-NATIVE-001");
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
        )
    };
    let max_ts = tape_values
        .iter()
        .filter_map(|value| value.get("ts_ns").and_then(|ts| ts.as_str()))
        .filter_map(|ts| ts.parse::<u64>().ok())
        .max()
        .unwrap_or(0);
    let mark_ts = native_mark_ts.max(max_ts);
    if let Some(mark) = final_mark {
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
    let output = json!({
        "status": "PASS",
        "tape": tape_values,
        "profile": "TAKER_L1_FUNDING_STREAMING",
        "events": count,
        "chunk_events": chunk,
        "iterations": result.iterations,
        "total_orders": result.total_orders,
        "total_positions": result.total_positions,
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
    });
    if let Some(path) = out_path {
        fs::write(path, serde_json::to_string_pretty(&output)? + "\n")?;
    } else {
        println!("{}", serde_json::to_string_pretty(&output)?);
    }
    Ok(())
}
