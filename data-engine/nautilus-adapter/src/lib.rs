//! Typed Arrow `RecordBatch` -> Nautilus `Vec<Data>` adapter.
//!
//! The production input is a real Arrow `RecordBatch` decoded from
//! Parquet (schema v2, see `qel-data-engine-ingest`), not a vector of
//! Python-dict-like rows and not JSONL. Conversion reads typed Arrow
//! arrays (`StringArray`/`UInt64Array`/`Int64Array`) directly and builds
//! pinned Nautilus `Data` objects in `source_ordinal` order for the
//! existing `add_data(Vec<Data>)` API. No Python `to_pylist()`, no
//! per-event JSON parsing, and no JSONL intermediate are involved.
//!
//! Zero-copy Arrow ingestion is NOT claimed: the pinned Nautilus build
//! (rev 27a8e54) has no Arrow/RecordBatch ingestion hook, so typed
//! `Vec<Data>` conversion is the integration boundary. That absence is
//! not a blocker for this path.
//!
//! Economic semantics mirror the legacy NDJSON runner
//! (`native/runner::parse_event_line`): price rounded to 2dp and qty
//! to 3dp via the core half-even rescale (identical to
//! `Decimal::round_dp` for exact inputs), `ts_event` and `ts_init`
//! both set to economic `ts_ns`, funding rate as `Decimal(coeff, 8)`
//! with interval 480. Funding injection timing stays with the caller
//! (drain by `ts_ns`), as in the legacy runner.
//!
//! Conversion fails closed on: wrong schema (names/types/order),
//! nulls in any column, unknown event kinds or aggressors, rescale
//! overflow, and timestamp/ordinal regressions (within a batch and,
//! for [`BatchConverter`], across batches).

use anyhow::{anyhow, bail, Result};
use arrow::array::{Array, Int64Array, RecordBatch, StringArray, UInt64Array};
use arrow::datatypes::DataType;
use nautilus_core::UnixNanos;
use nautilus_model::{
    data::{Data, FundingRateUpdate, MarkPriceUpdate, QuoteTick, TradeTick},
    enums::AggressorSide,
    identifiers::{InstrumentId, TradeId},
    types::{Price, Quantity},
};
use qel_data_engine_core::{price_coeff_1e8_to_2dp, qty_coeff_1e8_to_3dp};
use rust_decimal::Decimal;

/// Expected schema v2: (name, type) in canonical order.
pub fn expected_columns() -> Vec<(&'static str, DataType)> {
    vec![
        ("event_type", DataType::Utf8),
        ("ts_ns", DataType::UInt64),
        ("recv_ns", DataType::UInt64),
        ("source_ordinal", DataType::Int64),
        ("price_i", DataType::Int64),
        ("qty_i", DataType::Int64),
        ("bid_i", DataType::Int64),
        ("ask_i", DataType::Int64),
        ("bid_qty_i", DataType::Int64),
        ("ask_qty_i", DataType::Int64),
        ("aggressor", DataType::Utf8),
        ("trade_id", DataType::Utf8),
        ("source_id", DataType::Utf8),
        ("symbol", DataType::Utf8),
        ("fidelity", DataType::Utf8),
    ]
}

/// Validate that a batch carries exactly schema v2 (names, types,
/// order, no nulls). Fails closed with `PIT_SCHEMA_MISMATCH` /
/// `PIT_REQUIRED_NULL` style codes.
pub fn validate_batch_schema(batch: &RecordBatch) -> Result<()> {
    let schema = batch.schema();
    let expected = expected_columns();
    if schema.fields().len() != expected.len() {
        bail!(
            "PIT_SCHEMA_MISMATCH:fields={} expected={}",
            schema.fields().len(),
            expected.len()
        );
    }
    for (i, (name, dtype)) in expected.iter().enumerate() {
        let field = &schema.fields()[i];
        if field.name() != name || field.data_type() != dtype {
            bail!(
                "PIT_SCHEMA_MISMATCH:{} expected {name}:{:?} got {}:{:?}",
                i,
                dtype,
                field.name(),
                field.data_type()
            );
        }
        if batch.column(i).null_count() > 0 {
            bail!("PIT_REQUIRED_NULL:{name}");
        }
    }
    Ok(())
}

fn price_from_coeff(coeff_1e8: i64) -> Result<Price> {
    let c2 = price_coeff_1e8_to_2dp(coeff_1e8).map_err(|e| anyhow!("price rescale: {e:?}"))?;
    let sign = if c2 < 0 { "-" } else { "" };
    let a = c2.abs();
    Ok(Price::from(
        format!("{sign}{}.{:02}", a / 100, a % 100).as_str(),
    ))
}

fn qty_from_coeff(coeff_1e8: i64) -> Result<Quantity> {
    let c3 = qty_coeff_1e8_to_3dp(coeff_1e8).map_err(|e| anyhow!("qty rescale: {e:?}"))?;
    let sign = if c3 < 0 { "-" } else { "" };
    let a = c3.abs();
    Ok(Quantity::from(
        format!("{sign}{}.{:03}", a / 1000, a % 1000).as_str(),
    ))
}

fn str_col<'a>(batch: &'a RecordBatch, name: &str) -> Result<&'a StringArray> {
    let idx = batch
        .schema()
        .index_of(name)
        .map_err(|_| anyhow!("PIT_SCHEMA_MISMATCH:{name}"))?;
    batch
        .column(idx)
        .as_any()
        .downcast_ref::<StringArray>()
        .ok_or_else(|| anyhow!("PIT_SCHEMA_MISMATCH:{name}"))
}

fn u64_col<'a>(batch: &'a RecordBatch, name: &str) -> Result<&'a UInt64Array> {
    let idx = batch
        .schema()
        .index_of(name)
        .map_err(|_| anyhow!("PIT_SCHEMA_MISMATCH:{name}"))?;
    batch
        .column(idx)
        .as_any()
        .downcast_ref::<UInt64Array>()
        .ok_or_else(|| anyhow!("PIT_SCHEMA_MISMATCH:{name}"))
}

fn i64_col<'a>(batch: &'a RecordBatch, name: &str) -> Result<&'a Int64Array> {
    let idx = batch
        .schema()
        .index_of(name)
        .map_err(|_| anyhow!("PIT_SCHEMA_MISMATCH:{name}"))?;
    batch
        .column(idx)
        .as_any()
        .downcast_ref::<Int64Array>()
        .ok_or_else(|| anyhow!("PIT_SCHEMA_MISMATCH:{name}"))
}

/// Stateful converter that enforces timestamp/ordinal monotonicity
/// across RecordBatch boundaries. Feed batches in `source_ordinal`
/// order; the first regression fails closed.
#[derive(Default)]
pub struct BatchConverter {
    last_ts: Option<u64>,
    last_ordinal: Option<i64>,
}

impl BatchConverter {
    pub fn new() -> Self {
        Self::default()
    }

    /// Convert one RecordBatch to typed `Vec<Data>` in ordinal order.
    pub fn convert(&mut self, batch: &RecordBatch, id: InstrumentId) -> Result<Vec<Data>> {
        validate_batch_schema(batch)?;
        let kinds = str_col(batch, "event_type")?;
        let ts_ns = u64_col(batch, "ts_ns")?;
        let ordinals = i64_col(batch, "source_ordinal")?;
        let price_i = i64_col(batch, "price_i")?;
        let qty_i = i64_col(batch, "qty_i")?;
        let bid_i = i64_col(batch, "bid_i")?;
        let ask_i = i64_col(batch, "ask_i")?;
        let bid_qty_i = i64_col(batch, "bid_qty_i")?;
        let ask_qty_i = i64_col(batch, "ask_qty_i")?;
        let aggressors = str_col(batch, "aggressor")?;
        let trade_ids = str_col(batch, "trade_id")?;

        let mut out = Vec::with_capacity(batch.num_rows());
        for i in 0..batch.num_rows() {
            let ts = ts_ns.value(i);
            let ordinal = ordinals.value(i);
            if let Some(prev) = self.last_ts {
                if ts < prev {
                    bail!("EVENT_TS_REGRESSION");
                }
            }
            if let Some(prev) = self.last_ordinal {
                if ordinal <= prev {
                    bail!("ORDINAL_NOT_INCREASING");
                }
            }
            self.last_ts = Some(ts);
            self.last_ordinal = Some(ordinal);
            let ts_nanos = UnixNanos::from(ts);
            let data = match kinds.value(i) {
                "Quote" => Data::Quote(QuoteTick::new(
                    id,
                    price_from_coeff(bid_i.value(i))?,
                    price_from_coeff(ask_i.value(i))?,
                    qty_from_coeff(bid_qty_i.value(i))?,
                    qty_from_coeff(ask_qty_i.value(i))?,
                    ts_nanos,
                    ts_nanos,
                )),
                "Trade" => {
                    let side = match aggressors.value(i) {
                        "BUY" => AggressorSide::Buyer,
                        "SELL" => AggressorSide::Seller,
                        other => bail!("UNKNOWN_AGGRESSOR:{other}"),
                    };
                    Data::Trade(TradeTick::new(
                        id,
                        price_from_coeff(price_i.value(i))?,
                        qty_from_coeff(qty_i.value(i))?,
                        side,
                        TradeId::from(trade_ids.value(i)),
                        ts_nanos,
                        ts_nanos,
                    ))
                }
                "Mark" => Data::MarkPriceUpdate(MarkPriceUpdate::new(
                    id,
                    price_from_coeff(price_i.value(i))?,
                    ts_nanos,
                    ts_nanos,
                )),
                "Funding" => {
                    let rate = Decimal::new(price_i.value(i), 8);
                    Data::FundingRateUpdate(FundingRateUpdate::new(
                        id, rate, Some(480), None, ts_nanos, ts_nanos,
                    ))
                }
                other => bail!("UNKNOWN_KIND:{other}"),
            };
            out.push(data);
        }
        Ok(out)
    }
}

/// Convert a single batch with a fresh converter (within-batch checks
/// only). Prefer [`BatchConverter`] when streaming multiple batches.
pub fn batch_to_data(batch: &RecordBatch, id: InstrumentId) -> Result<Vec<Data>> {
    BatchConverter::new().convert(batch, id)
}

/// Last Quote mid price in a batch as `f64`, from typed bid/ask arrays:
/// `(bid_i + ask_i) / 2` at scale 1e8. Mirrors the legacy runner's
/// `final_mid` tracking (last quote wins); `None` if no Quote row.
pub fn batch_last_quote_mid(batch: &RecordBatch) -> Option<f64> {
    let kinds = str_col(batch, "event_type").ok()?;
    let bid_i = i64_col(batch, "bid_i").ok()?;
    let ask_i = i64_col(batch, "ask_i").ok()?;
    let mut last: Option<f64> = None;
    for i in 0..batch.num_rows() {
        if kinds.value(i) == "Quote" {
            last = Some((bid_i.value(i) as f64 + ask_i.value(i) as f64) / 2.0 / 1e8);
        }
    }
    last
}

#[cfg(test)]
mod tests {
    use super::*;
    use arrow::array::{Int64Builder, StringBuilder, UInt64Builder};
    use arrow::datatypes::{Field, Schema};
    use std::sync::Arc;

    fn schema() -> Schema {
        Schema::new(
            expected_columns()
                .into_iter()
                .map(|(n, t)| Field::new(n, t, false))
                .collect::<Vec<_>>(),
        )
    }

    #[allow(clippy::too_many_arguments)]
    fn batch(
        kinds: Vec<&str>,
        ts: Vec<u64>,
        ordinals: Vec<i64>,
        price: Vec<i64>,
        qty: Vec<i64>,
        bid: Vec<i64>,
        ask: Vec<i64>,
        bid_qty: Vec<i64>,
        ask_qty: Vec<i64>,
        aggressors: Vec<&str>,
        trade_ids: Vec<&str>,
    ) -> RecordBatch {
        let s = schema();
        let sb = |vals: &Vec<&str>| -> Arc<dyn Array> {
            let mut b = StringBuilder::new();
            for v in vals {
                b.append_value(v);
            }
            Arc::new(b.finish())
        };
        let ub = |vals: &Vec<u64>| -> Arc<dyn Array> {
            let mut b = UInt64Builder::new();
            for v in vals {
                b.append_value(*v);
            }
            Arc::new(b.finish())
        };
        let ib = |vals: &Vec<i64>| -> Arc<dyn Array> {
            let mut b = Int64Builder::new();
            for v in vals {
                b.append_value(*v);
            }
            Arc::new(b.finish())
        };
        let n = kinds.len();
        let zeros_u64: Vec<u64> = vec![0; n];
        let cols: Vec<Arc<dyn Array>> = vec![
            sb(&kinds),
            ub(&ts),
            ub(&zeros_u64),
            ib(&ordinals),
            ib(&price),
            ib(&qty),
            ib(&bid),
            ib(&ask),
            ib(&bid_qty),
            ib(&ask_qty),
            sb(&aggressors),
            sb(&trade_ids),
            sb(&vec!["BINANCE_USDM_PUBLIC_ARCHIVE"; n]),
            sb(&vec!["BTCUSDT"; n]),
            sb(&vec!["observed_trade"; n]),
        ];
        RecordBatch::try_new(Arc::new(s), cols).unwrap()
    }

    fn fixture() -> RecordBatch {
        // Quote bid 100.00 ask 100.02 qty 1.000; Trade 100.01 x 0.250 BUY;
        // Mark 100.05; Funding 0.00010000.
        batch(
            vec!["Quote", "Trade", "Mark", "Funding"],
            vec![1000, 1000, 2000, 3000],
            vec![0, 1, 2, 3],
            vec![0, 10_001_000_000, 10_005_000_000, 10_000],
            vec![0, 25_000_000, 0, 0],
            vec![10_000_000_000, 0, 0, 0],
            vec![10_002_000_000, 0, 0, 0],
            vec![100_000_000, 0, 0, 0],
            vec![100_000_000, 0, 0, 0],
            vec!["", "BUY", "", ""],
            vec!["", "42", "", ""],
        )
    }

    #[test]
    fn converts_all_kinds_with_exact_values() {
        let id = InstrumentId::from("BTCUSDT-PERP.BINANCE");
        let data = batch_to_data(&fixture(), id).unwrap();
        assert_eq!(data.len(), 4);
        match &data[0] {
            Data::Quote(q) => {
                assert_eq!(q.bid_price.as_f64(), 100.00);
                assert_eq!(q.ask_price.as_f64(), 100.02);
                assert_eq!(q.bid_size.as_f64(), 1.0);
                assert_eq!(q.ask_size.as_f64(), 1.0);
                assert_eq!(q.ts_event.as_u64(), 1000);
            }
            other => panic!("expected Quote, got {other:?}"),
        }
        match &data[1] {
            Data::Trade(t) => {
                assert_eq!(t.price.as_f64(), 100.01);
                assert_eq!(t.size.as_f64(), 0.25);
                assert_eq!(t.aggressor_side, AggressorSide::Buyer);
                assert_eq!(t.trade_id.to_string(), "42");
            }
            other => panic!("expected Trade, got {other:?}"),
        }
        match &data[2] {
            Data::MarkPriceUpdate(m) => assert_eq!(m.value.as_f64(), 100.05),
            other => panic!("expected Mark, got {other:?}"),
        }
        match &data[3] {
            Data::FundingRateUpdate(f) => {
                assert_eq!(f.rate, Decimal::new(10_000, 8));
                assert_eq!(f.interval, Some(480));
            }
            other => panic!("expected Funding, got {other:?}"),
        }
    }

    #[test]
    fn price_rounding_is_half_even() {
        let id = InstrumentId::from("BTCUSDT-PERP.BINANCE");
        // Trade price 100.005 at 1e8 -> 100.00 (half-even); 100.015 -> 100.02.
        let b = batch(
            vec!["Trade", "Trade"],
            vec![1, 2],
            vec![0, 1],
            vec![10_000_500_000, 10_001_500_000],
            vec![100_000_000, 100_000_000],
            vec![0, 0],
            vec![0, 0],
            vec![0, 0],
            vec![0, 0],
            vec!["BUY", "SELL"],
            vec!["1", "2"],
        );
        let data = batch_to_data(&b, id).unwrap();
        match &data[0] {
            Data::Trade(t) => assert_eq!(t.price.as_f64(), 100.00),
            other => panic!("expected Trade, got {other:?}"),
        }
        match &data[1] {
            Data::Trade(t) => {
                assert_eq!(t.price.as_f64(), 100.02);
                assert_eq!(t.aggressor_side, AggressorSide::Seller);
            }
            other => panic!("expected Trade, got {other:?}"),
        }
    }

    #[test]
    fn rejects_unknown_kind_and_regressions() {
        let id = InstrumentId::from("BTCUSDT-PERP.BINANCE");
        let unknown = batch(
            vec!["Bogus"],
            vec![1],
            vec![0],
            vec![0],
            vec![0],
            vec![0],
            vec![0],
            vec![0],
            vec![0],
            vec![""],
            vec![""],
        );
        assert!(batch_to_data(&unknown, id).is_err());
        let regressed = batch(
            vec!["Trade", "Trade"],
            vec![2000, 1000],
            vec![0, 1],
            vec![10_000_000_000, 10_000_000_000],
            vec![100_000_000, 100_000_000],
            vec![0, 0],
            vec![0, 0],
            vec![0, 0],
            vec![0, 0],
            vec!["BUY", "BUY"],
            vec!["1", "2"],
        );
        assert!(batch_to_data(&regressed, id).is_err());
        // Cross-batch regression via the stateful converter.
        let mut conv = BatchConverter::new();
        let first = batch(
            vec!["Trade"],
            vec![5000],
            vec![10],
            vec![10_000_000_000],
            vec![100_000_000],
            vec![0],
            vec![0],
            vec![0],
            vec![0],
            vec!["BUY"],
            vec!["1"],
        );
        conv.convert(&first, id).unwrap();
        let second = batch(
            vec!["Trade"],
            vec![4000],
            vec![11],
            vec![10_000_000_000],
            vec![100_000_000],
            vec![0],
            vec![0],
            vec![0],
            vec![0],
            vec!["BUY"],
            vec!["2"],
        );
        assert!(conv.convert(&second, id).is_err());
    }

    #[test]
    fn rejects_wrong_schema() {
        let id = InstrumentId::from("BTCUSDT-PERP.BINANCE");
        let good = fixture();
        // Drop the last column: schema validation must fail closed.
        let bad = RecordBatch::try_new(
            Arc::new(Schema::new(vec![Field::new("event_type", DataType::Utf8, false)])),
            vec![good.column(0).clone()],
        )
        .unwrap();
        assert!(batch_to_data(&bad, id).is_err());
    }

    #[test]
    fn last_quote_mid_from_typed_arrays() {
        assert_eq!(batch_last_quote_mid(&fixture()), Some(100.01));
    }
}
