//! Typed Arrow batch -> Nautilus `Vec<Data>` adapter (P2, IMPLEMENTED_UNVERIFIED).
//!
//! Inputs are the schema-v2 columns as primitive slices (already decoded
//! from a `RecordBatch` by the caller/reader); outputs are pinned
//! `Data` objects in input (source_ordinal) order. This eliminates the
//! JSON roundtrip; it does NOT claim zero-copy Arrow ingestion (that hook
//! is absent in pinned Nautilus and is tracked as BLOCKED_BACKEND for the
//! zero-copy capability only).
//!
//! Economic semantics mirror `native/runner::parse_event_line`: price
//! rounded to 2dp, qty to 3dp (via core half-even rescale), ts_event and
//! ts_init both set to economic `ts_ns`; funding injection timing is the
//! caller's responsibility (drain by ts), as in the legacy runner.
//!
//! Cargo verification of this crate is deferred to an idle host
//! (BLOCKED_VALIDATION for the compile step); the pure rescale logic in
//! `qel-data-engine-core` is tested with `cargo test` in this PR.

use anyhow::{anyhow, Result};
use nautilus_core::UnixNanos;
use nautilus_model::{
    data::{Data, FundingRateUpdate, MarkPriceUpdate, QuoteTick, TradeTick},
    enums::AggressorSide,
    identifiers::{InstrumentId, TradeId},
    types::{Price, Quantity},
};
use qel_data_engine_core::{price_coeff_1e8_to_2dp, qty_coeff_1e8_to_3dp};
use rust_decimal::Decimal;

/// One schema-v2 row, decoded from a RecordBatch column slice.
#[derive(Clone, Debug)]
pub struct EventRow {
    pub event_type: String,
    pub ts_ns: u64,
    pub price_i: i64,
    pub qty_i: i64,
    pub bid_i: i64,
    pub ask_i: i64,
    pub bid_qty_i: i64,
    pub ask_qty_i: i64,
    pub aggressor: String,
    pub trade_id: String,
}

fn price_from_coeff(coeff_1e8: i64) -> Result<Price> {
    let c2 = price_coeff_1e8_to_2dp(coeff_1e8).map_err(|e| anyhow!("price rescale: {e:?}"))?;
    // Format with exactly 2dp to match Price precision semantics.
    let sign = if c2 < 0 { "-" } else { "" };
    let a = c2.abs();
    Ok(Price::from(format!("{sign}{}.{:02}", a / 100, a % 100).as_str()))
}

fn qty_from_coeff(coeff_1e8: i64) -> Result<Quantity> {
    let c3 = qty_coeff_1e8_to_3dp(coeff_1e8).map_err(|e| anyhow!("qty rescale: {e:?}"))?;
    let sign = if c3 < 0 { "-" } else { "" };
    let a = c3.abs();
    Ok(Quantity::from(format!("{sign}{}.{:03}", a / 1000, a % 1000).as_str()))
}

/// Convert one batch (rows in source_ordinal order) to typed Data.
/// Fails closed on unknown kinds, out-of-order timestamps, or rescale errors.
pub fn batch_to_data(rows: &[EventRow], id: InstrumentId) -> Result<Vec<Data>> {
    let mut out = Vec::with_capacity(rows.len());
    let mut last_ts: Option<u64> = None;
    for row in rows {
        if let Some(prev) = last_ts {
            if row.ts_ns < prev {
                return Err(anyhow!("EVENT_TS_REGRESSION"));
            }
        }
        last_ts = Some(row.ts_ns);
        let ts = UnixNanos::from(row.ts_ns);
        let data = match row.event_type.as_str() {
            "Quote" => Data::Quote(QuoteTick::new(
                id,
                price_from_coeff(row.bid_i)?,
                price_from_coeff(row.ask_i)?,
                qty_from_coeff(row.bid_qty_i)?,
                qty_from_coeff(row.ask_qty_i)?,
                ts,
                ts,
            )),
            "Trade" => {
                let side = if row.aggressor == "SELL" {
                    AggressorSide::Seller
                } else {
                    AggressorSide::Buyer
                };
                Data::Trade(TradeTick::new(
                    id,
                    price_from_coeff(row.price_i)?,
                    qty_from_coeff(row.qty_i)?,
                    side,
                    TradeId::from(row.trade_id.as_str()),
                    ts,
                    ts,
                ))
            }
            "Mark" => Data::MarkPriceUpdate(MarkPriceUpdate::new(id, price_from_coeff(row.price_i)?, ts, ts)),
            "Funding" => {
                let rate = Decimal::new(row.price_i, 8);
                Data::FundingRateUpdate(FundingRateUpdate::new(id, rate, Some(480), None, ts, ts))
            }
            other => return Err(anyhow!("UNKNOWN_KIND:{other}")),
        };
        out.push(data);
    }
    Ok(out)
}
