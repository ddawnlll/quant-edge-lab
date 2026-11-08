//! QEL native L1 taker runner: Nautilus replay driven by the typed policy DAG.

use std::{collections::HashMap, fmt::Debug, fs, sync::{Arc, Mutex}};

use anyhow::{anyhow, bail, Context, Result};
use nautilus_backtest::{
    config::{BacktestEngineConfig, SimulatedVenueConfig},
    engine::BacktestEngine,
};
use nautilus_common::actor::DataActor;
use nautilus_core::UnixNanos;
use nautilus_model::{
    data::{Data, FundingRateUpdate, MarkPriceUpdate, QuoteTick, TradeTick},
    enums::{AccountType, AggressorSide, BookType, OmsType, OrderSide, TimeInForce},
    events::{OrderFilled, PositionClosed, PositionOpened},
    identifiers::{InstrumentId, StrategyId, Symbol, TradeId, Venue},
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

fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().collect();
    let mut events = None;
    let mut strategy_path = None;
    let mut out_path = None;
    let mut notional = 100.0f64;
    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--events" => { events = args.get(i + 1).cloned(); i += 2; }
            "--strategy" => { strategy_path = args.get(i + 1).cloned(); i += 2; }
            "--out" => { out_path = args.get(i + 1).cloned(); i += 2; }
            "--notional" => { notional = args.get(i + 1).ok_or_else(|| anyhow!("notional"))?.parse()?; i += 2; }
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
    engine.add_data(data, None, true, true)?;
    engine.run(None, None, None, false)?;
    let result = engine.get_result();
    let mut tape = Vec::new();
    {
        let cache = engine.kernel().cache.borrow();
        let sid = StrategyId::from("TAKER-NATIVE-001");
        let positions = cache.positions_open(None, Some(&id), Some(&sid), None, None);
        for p in positions {
            for adj in &p.adjustments {
                if let Some(money) = adj.pnl_change {
                    tape.push(json!({"type": "FUNDING", "amount": trim(money.as_f64(), 8), "epoch": "1"}));
                }
            }
        }
    }
    let mut tape_values = tape_handle.lock().unwrap().clone();
    if let Some(mid) = final_mid {
        tape_values.push(json!({"type": "MARK", "price": trim(mid, 2)}));
    }
    tape_values.push(json!({"type": "END_FOLD", "close_cost": "0"}));
    let output = json!({
        "status": "PASS",
        "tape": tape_values,
        "profile": "TAKER_L1_FUNDING_STREAMING",
        "events": count,
        "iterations": result.iterations,
        "total_orders": result.total_orders,
        "total_positions": result.total_positions,
        "pnl_total": result.summary.get("PnL (total)").cloned(),
        "tape_extra": tape,
        "events_path": events_path,
    });
    if let Some(path) = out_path {
        fs::write(path, serde_json::to_string_pretty(&output)? + "\n")?;
    } else {
        println!("{}", serde_json::to_string_pretty(&output)?);
    }
    Ok(())
}

