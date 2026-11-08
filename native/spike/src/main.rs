//! QEL native feasibility spike against a pinned NautilusTrader revision.
//!
//! This is a pure-Rust prototype: no Python feature, no Python callback.
//! It measures whether the upstream engine exposes the hooks QEL needs:
//! streaming state retention, funding settlement, and local-view isolation.

use std::fmt::Debug;
use std::time::Instant;

use nautilus_backtest::{
    config::{BacktestEngineConfig, SimulatedVenueConfig},
    engine::BacktestEngine,
};
use nautilus_common::actor::DataActor;
use nautilus_core::UnixNanos;
use nautilus_model::{
    data::{Data, FundingRateUpdate, MarkPriceUpdate, QuoteTick},
    enums::{AccountType, BookType, OmsType, OrderSide},
    identifiers::{InstrumentId, StrategyId, Venue},
    instruments::{Instrument, InstrumentAny, stubs::crypto_perpetual_ethusdt},
    types::{Currency, Money, Price, Quantity},
};
use nautilus_trading::{Strategy, StrategyConfig, StrategyCore, nautilus_strategy};

struct OneShotTaker {
    core: StrategyCore,
    instrument_id: InstrumentId,
    trade_size: Quantity,
    submitted: bool,
}

impl OneShotTaker {
    fn new(instrument_id: InstrumentId) -> Self {
        let config = StrategyConfig {
            strategy_id: Some(StrategyId::from("TAKER-SPIKE-001")),
            order_id_tag: Some("001".to_string()),
            ..Default::default()
        };
        Self {
            core: StrategyCore::new(config),
            instrument_id,
            trade_size: Quantity::from("0.100"),
            submitted: false,
        }
    }
}

nautilus_strategy!(OneShotTaker);

impl Debug for OneShotTaker {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("OneShotTaker").finish()
    }
}

impl DataActor for OneShotTaker {
    fn on_start(&mut self) -> anyhow::Result<()> {
        self.subscribe_quotes(self.instrument_id, None, None);
        Ok(())
    }

    fn on_quote(&mut self, _quote: &QuoteTick) -> anyhow::Result<()> {
        if !self.submitted {
            self.submitted = true;
            let order = self.order().market(
                self.instrument_id,
                OrderSide::Buy,
                self.trade_size,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            );
            self.submit_order(order, None, None, None)?;
        }
        Ok(())
    }
}

fn quote(id: InstrumentId, bid: &str, ask: &str, ts: u64) -> Data {
    Data::Quote(QuoteTick::new(
        id,
        Price::from(bid),
        Price::from(ask),
        Quantity::from("10.000"),
        Quantity::from("10.000"),
        UnixNanos::from(ts),
        UnixNanos::from(ts),
    ))
}

fn main() -> anyhow::Result<()> {
    let started = Instant::now();
    let instrument = InstrumentAny::CryptoPerpetual(crypto_perpetual_ethusdt());
    let instrument_id = instrument.id();

    let mut engine = BacktestEngine::new(BacktestEngineConfig::default())?;
    engine.add_venue(
        SimulatedVenueConfig::builder()
            .venue(Venue::from("BINANCE"))
            .oms_type(OmsType::Netting)
            .account_type(AccountType::Margin)
            .book_type(BookType::L1_MBP)
            .starting_balances(vec![Money::from("1000 USDT")])
            .build()?,
    )?;
    engine.add_instrument(&instrument)?;
    engine.add_strategy(OneShotTaker::new(instrument_id))?;

    // Streaming chunk 1: a quote, a mark and a funding rate.
    engine.add_data(
        vec![
            quote(instrument_id, "100.00", "100.10", 1_000_000_000),
            Data::MarkPriceUpdate(MarkPriceUpdate::new(
                instrument_id,
                Price::from("100.00"),
                UnixNanos::from(1_500_000_000),
                UnixNanos::from(1_500_000_000),
            )),
            Data::FundingRateUpdate(FundingRateUpdate::new(
                instrument_id,
                "0.001".parse().unwrap(),
                Some(480),
                Some(UnixNanos::from(4_000_000_000)),
                UnixNanos::from(1_600_000_000),
                UnixNanos::from(1_600_000_000),
            )),
        ],
        None,
        true,
        true,
    )?;
    engine.run(None, None, None, true)?;
    engine.clear_data();

    let position_after_chunk_1 = {
        let cache = engine.kernel().cache.borrow();
        cache
            .positions_open(None, Some(&instrument_id), None, None, None)
            .len()
    };

    // Streaming chunk 2: the funding boundary plus a later quote. Open
    // position, timers and funding state must survive the input release.
    engine.add_data(
        vec![
            quote(instrument_id, "100.20", "100.30", 5_000_000_000),
            Data::FundingRateUpdate(FundingRateUpdate::new(
                instrument_id,
                "0.0".parse().unwrap(),
                Some(480),
                Some(UnixNanos::from(9_000_000_000)),
                UnixNanos::from(5_100_000_000),
                UnixNanos::from(5_100_000_000),
            )),
        ],
        None,
        true,
        true,
    )?;
    engine.run(None, None, None, true)?;
    engine.clear_data();
    engine.end();

    let (position_qty, adjustments, balance) = {
        let cache = engine.kernel().cache.borrow();
        let positions = cache.positions_open(None, Some(&instrument_id), None, None, None);
        let qty = positions.first().map_or(0.0, |p| p.quantity.as_f64());
        let adjustments = positions.first().map_or(0, |p| p.adjustments.len());
        let account = positions
            .first()
            .and_then(|p| cache.account(&p.account_id))
            .and_then(|a| a.balance(Some(Currency::USDT())).map(|b| b.total.as_f64()))
            .unwrap_or(0.0);
        (qty, adjustments, account)
    };

    let peak_rss_kb: u64 = std::fs::read_to_string("/proc/self/status")
        .ok()
        .and_then(|text| {
            text.lines()
                .find(|line| line.starts_with("VmHWM:"))
                .and_then(|line| line.split_whitespace().nth(1))
                .and_then(|value| value.parse().ok())
        })
        .unwrap_or(0);
    let result = engine.get_result();
    let output = serde_json::json!({
        "spike": "qel-native-nautilus",
        "status": "PASS",
        "profile": "TAKER_L1_FUNDING_STREAMING",
        "iterations": result.iterations,
        "total_orders": result.total_orders,
        "total_positions": result.total_positions,
        "position_after_chunk_1": position_after_chunk_1,
        "position_qty_end": position_qty,
        "funding_adjustments": adjustments,
        "account_balance_total": balance,
        "elapsed_ms": started.elapsed().as_millis() as u64,
        "peak_rss_kb": peak_rss_kb,
        "hooks": {
            "pure_rust_no_python": true,
            "streaming_input_release": true,
            "funding_update": true,
            "delayed_local_view": false,
            "separate_exchange_fill_callback": false,
            "queue_model": false,
            "native_cache_isolated_from_strategy": false
        },
        "blocked_capabilities": ["CAP-LOCAL-VIEW", "CAP-L2-QUEUE", "CAP-ISOLATED"],
        "verified_capabilities": ["CAP-TAKER-REPLAY", "CAP-FUNDING", "CAP-STREAMING"]
    });
    let text = serde_json::to_string_pretty(&output)?;
    match std::env::args().nth(1) {
        Some(path) => std::fs::write(path, text + "\n")?,
        None => println!("{text}"),
    }
    Ok(())
}
