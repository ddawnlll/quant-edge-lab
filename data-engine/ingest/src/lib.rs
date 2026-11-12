//! Rust-native Binance archive ZIP -> typed Parquet ingestion (schema v2).
//!
//! This is the production ingestion engine. It reuses the canonical
//! ordering and economic semantics of the legacy builders
//! (`scripts/m2_build_fold_tapes_stream.py`, byte-identical to
//! `qel.prepare.archive`) and the Python reference converter
//! (`qel.prepare.archive_parquet`, PR #49, retained as an independent
//! reference only):
//!
//! * synthetic L1 quote per second around the last trade (tick 0.01,
//!   qty 1.000), fidelity `assumed_synthetic_l1_around_trade`;
//! * tie order: Quote/Trade (archive order) before Mark before Funding
//!   at equal `ts_ns` (strict `<` flush of pending marks/funding);
//! * funding from the monthly `fundingRate` ZIP only; a missing funding
//!   ZIP is a loud failure, never a silent gap;
//! * checked fixed-point conversion at scale 1e8 with no float
//!   (see `qel-data-engine-core`); quantities are quantized to 3dp
//!   half-even before scaling, exactly as the canonical builders do;
//! * archive `transact_time` must be non-decreasing; out-of-order input
//!   fails closed instead of being silently sorted;
//! * sealed reserve (day >= 2026-03-08) is refused.
//!
//! Output schema v2 (exact match with the Python reference):
//! `event_type` string, `ts_ns` uint64, `recv_ns` uint64,
//! `source_ordinal` int64, `price_i`/`qty_i`/`bid_i`/`ask_i`/
//! `bid_qty_i`/`ask_qty_i` int64, `aggressor`/`trade_id`/`source_id`/
//! `symbol`/`fidelity` string. `recv_ns == ts_ns` (assumed zero
//! latency, recorded in the manifest).

use std::collections::BTreeMap;
use std::fs::File;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::sync::Arc;

use anyhow::{anyhow, bail, Result};
use arrow::array::{Int64Builder, RecordBatch, StringBuilder, UInt64Builder};
use arrow::datatypes::{DataType, Field, Schema};
use parquet::arrow::ArrowWriter;
use parquet::file::properties::WriterProperties;
use parquet::file::reader::FileReader;
use qel_data_engine_core::{parse_decimal_coeff_1e8, parse_decimal_qty_coeff_1e8};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

pub const SEALED_START: &str = "2026-03-08";
pub const SCALE: u32 = 8;
pub const DEFAULT_BATCH_ROWS: usize = 65536;
pub const SOURCE_ID: &str = "BINANCE_USDM_PUBLIC_ARCHIVE";
/// 0.01 at scale 1e8.
pub const TICK_I: i64 = 1_000_000;
/// 1.000 at scale 1e8.
pub const QUOTE_QTY_I: i64 = 100_000_000;

pub const FIDELITY_QUOTE: &str = "assumed_synthetic_l1_around_trade";
pub const FIDELITY_TRADE: &str = "observed_trade";
pub const FIDELITY_MARK: &str = "observed_mark_price_kline_close";
pub const FIDELITY_FUNDING: &str = "observed_venue_funding";

/// Schema v2 field (name, type) pairs in canonical order. The nautilus
/// adapter validates the same names/types/order on read.
pub fn parquet_schema() -> Schema {
    Schema::new(vec![
        Field::new("event_type", DataType::Utf8, false),
        Field::new("ts_ns", DataType::UInt64, false),
        Field::new("recv_ns", DataType::UInt64, false),
        Field::new("source_ordinal", DataType::Int64, false),
        Field::new("price_i", DataType::Int64, false),
        Field::new("qty_i", DataType::Int64, false),
        Field::new("bid_i", DataType::Int64, false),
        Field::new("ask_i", DataType::Int64, false),
        Field::new("bid_qty_i", DataType::Int64, false),
        Field::new("ask_qty_i", DataType::Int64, false),
        Field::new("aggressor", DataType::Utf8, false),
        Field::new("trade_id", DataType::Utf8, false),
        Field::new("source_id", DataType::Utf8, false),
        Field::new("symbol", DataType::Utf8, false),
        Field::new("fidelity", DataType::Utf8, false),
    ])
}

pub struct ConvertConfig {
    pub agg_zip: PathBuf,
    pub mark_zip: PathBuf,
    pub funding_zip: PathBuf,
    pub out_path: PathBuf,
    pub day: String,
    pub symbol: String,
    pub batch_rows: usize,
}

impl Default for ConvertConfig {
    fn default() -> Self {
        Self {
            agg_zip: PathBuf::new(),
            mark_zip: PathBuf::new(),
            funding_zip: PathBuf::new(),
            out_path: PathBuf::new(),
            day: String::new(),
            symbol: "BTCUSDT".to_string(),
            batch_rows: DEFAULT_BATCH_ROWS,
        }
    }
}

fn sha256_file(path: &Path) -> Result<String> {
    let mut file = File::open(path).map_err(|e| anyhow!("ZIP_MISSING:{}: {e}", path.display()))?;
    let mut hasher = Sha256::new();
    let mut buf = vec![0u8; 1 << 20];
    loop {
        let n = file.read(&mut buf)?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    Ok(format!("{:x}", hasher.finalize()))
}

/// Days since 1970-01-01 for a civil date (Howard Hinnant's algorithm).
/// Returns None for invalid dates.
pub fn days_from_civil(year: i64, month: u32, day: u32) -> Option<i64> {
    if !(1..=12).contains(&month) || day == 0 || day > 31 {
        return None;
    }
    let y = if month <= 2 { year - 1 } else { year };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = (y - era * 400) as u64; // [0, 399]
    let mp = if month > 2 { month - 3 } else { month + 9 } as u64; // [0, 11]
    let doy = (153 * mp + 2) / 5 + (day as u64) - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    let days = era * 146097 + doe as i64 - 719468;
    // Reject impossible dates such as 2024-02-31 by round-tripping the
    // month length.
    let dim = match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 => {
            if (year % 4 == 0 && year % 100 != 0) || year % 400 == 0 {
                29
            } else {
                28
            }
        }
        _ => return None,
    };
    if day > dim {
        return None;
    }
    Some(days)
}

pub fn parse_day_start_ms(day: &str) -> Result<i64> {
    let parts: Vec<&str> = day.split('-').collect();
    if parts.len() != 3 || parts[0].len() != 4 || parts[1].len() != 2 || parts[2].len() != 2 {
        bail!("INVALID_DAY:{day}");
    }
    let year: i64 = parts[0].parse().map_err(|_| anyhow!("INVALID_DAY:{day}"))?;
    let month: u32 = parts[1].parse().map_err(|_| anyhow!("INVALID_DAY:{day}"))?;
    let dom: u32 = parts[2].parse().map_err(|_| anyhow!("INVALID_DAY:{day}"))?;
    let days = days_from_civil(year, month, dom).ok_or_else(|| anyhow!("INVALID_DAY:{day}"))?;
    Ok(days * 86_400_000)
}

fn ts_ms_to_ns(ts_ms: i64) -> Result<u64> {
    if ts_ms < 0 {
        bail!("TRADE_TS_INVALID:{ts_ms}");
    }
    let ns = (ts_ms as u64)
        .checked_mul(1_000_000)
        .ok_or_else(|| anyhow!("TS_OVERFLOW:{ts_ms}"))?;
    Ok(ns)
}

fn coeff(text: &str, field: &str) -> Result<i64> {
    parse_decimal_coeff_1e8(text).map_err(|e| anyhow!("DECIMAL_{e:?}:{field}={text}"))
}

fn qty_coeff(text: &str, field: &str) -> Result<i64> {
    parse_decimal_qty_coeff_1e8(text).map_err(|e| anyhow!("DECIMAL_{e:?}:{field}={text}"))
}

/// Open a ZIP archive, failing closed on missing/unreadable/empty input.
fn open_zip_archive(zip_path: &Path) -> Result<zip::ZipArchive<File>> {
    if !zip_path.is_file() {
        bail!("ZIP_MISSING:{}", zip_path.display());
    }
    let file = File::open(zip_path).map_err(|e| anyhow!("ZIP_UNREADABLE:{}: {e}", zip_path.display()))?;
    let archive = zip::ZipArchive::new(file)
        .map_err(|e| anyhow!("ZIP_UNREADABLE:{}: {e}", zip_path.display()))?;
    if archive.len() == 0 {
        bail!("ZIP_EMPTY:{}", zip_path.display());
    }
    Ok(archive)
}

/// Open the first entry of an already-open ZIP as a CSV reader and
/// validate headers. The returned reader borrows `archive`; the archive
/// must outlive the reader (streaming, no CSV materialization).
fn zip_csv_reader<'a>(
    archive: &'a mut zip::ZipArchive<File>,
    zip_path: &Path,
    required: &[&str],
) -> Result<(
    csv::Reader<std::io::BufReader<zip::read::ZipFile<'a, File>>>,
    BTreeMap<String, usize>,
)> {
    let entry = archive
        .by_index(0)
        .map_err(|e| anyhow!("ZIP_UNREADABLE:{}: {e}", zip_path.display()))?;
    let mut reader = csv::ReaderBuilder::new()
        .has_headers(true)
        .flexible(false)
        .from_reader(std::io::BufReader::with_capacity(1 << 20, entry));
    let headers = reader
        .headers()
        .map_err(|e| anyhow!("CSV_NO_HEADER:{}: {e}", zip_path.display()))?
        .clone();
    let mut index: BTreeMap<String, usize> = BTreeMap::new();
    for (i, name) in headers.iter().enumerate() {
        index.insert(name.to_string(), i);
    }
    for name in required {
        if !index.contains_key(*name) {
            bail!("CSV_MISSING_COLUMNS:[{name}] in {}", zip_path.display());
        }
    }
    Ok((reader, index))
}

fn field<'a>(record: &'a csv::StringRecord, index: &BTreeMap<String, usize>, name: &str) -> Result<&'a str> {
    let i = *index.get(name).ok_or_else(|| anyhow!("CSV_MISSING_COLUMNS:[{name}]"))?;
    record.get(i).ok_or_else(|| anyhow!("CSV_FIELD_MISSING:{name}"))
}

fn parse_ts_ms(text: &str, what: &str) -> Result<i64> {
    text.trim()
        .parse::<i64>()
        .map_err(|_| anyhow!("{what}_TS_INVALID:{text}"))
}

fn require_zero(text: &str, column: &str, path: &Path) -> Result<()> {
    let value: f64 = text
        .trim()
        .parse()
        .map_err(|_| anyhow!("MARK_SOURCE_MISSING:{} non-numeric {column}={text}", path.display()))?;
    if value != 0.0 {
        bail!(
            "MARK_SOURCE_MISSING:{} looks like trade klines ({column}={text}); fetch markPriceKlines, not klines",
            path.display()
        );
    }
    Ok(())
}

fn load_marks(mark_zip: &Path) -> Result<Vec<(i64, i64)>> {
    let mut archive = open_zip_archive(mark_zip)?;
    let (mut reader, index) = zip_csv_reader(
        &mut archive,
        mark_zip,
        &["close_time", "close", "volume", "quote_volume"],
    )?;
    let mut rows = Vec::new();
    for result in reader.records() {
        let record = result.map_err(|e| anyhow!("CSV_RECORD_INVALID:{e}"))?;
        // markPriceKlines always has zero volume/quote_volume; the 1m trade
        // klines do not. Fail closed instead of labelling trade close as mark.
        require_zero(field(&record, &index, "volume")?, "volume", mark_zip)?;
        require_zero(field(&record, &index, "quote_volume")?, "quote_volume", mark_zip)?;
        let ts_ms = parse_ts_ms(field(&record, &index, "close_time")?, "MARK")?;
        let price_i = coeff(field(&record, &index, "close")?, "mark.close")?;
        rows.push((ts_ms, price_i));
    }
    if rows.is_empty() {
        bail!("MARK_SOURCE_MISSING:{} has no rows", mark_zip.display());
    }
    rows.sort_by_key(|r| r.0);
    Ok(rows)
}

fn load_funding(funding_zip: &Path) -> Result<Vec<(i64, i64)>> {
    if !funding_zip.is_file() {
        bail!(
            "FUNDING_ZIP_MISSING:{} (monthly fundingRate required)",
            funding_zip.display()
        );
    }
    let mut archive = open_zip_archive(funding_zip)?;
    let (mut reader, index) =
        zip_csv_reader(&mut archive, funding_zip, &["calc_time", "last_funding_rate"])?;
    let mut rows = Vec::new();
    for result in reader.records() {
        let record = result.map_err(|e| anyhow!("CSV_RECORD_INVALID:{e}"))?;
        let ts_ms = parse_ts_ms(field(&record, &index, "calc_time")?, "FUNDING")?;
        let rate_i = coeff(field(&record, &index, "last_funding_rate")?, "funding.last_funding_rate")?;
        rows.push((ts_ms, rate_i));
    }
    rows.sort_by_key(|r| r.0);
    Ok(rows)
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum PendingKind {
    Mark,
    Funding,
}

struct Pending {
    ts_ms: i64,
    order: u8, // 0 = Mark, 1 = Funding
    kind: PendingKind,
    coeff: i64,
}

#[derive(Default)]
struct Columns {
    event_type: Vec<String>,
    ts_ns: Vec<u64>,
    recv_ns: Vec<u64>,
    source_ordinal: Vec<i64>,
    price_i: Vec<i64>,
    qty_i: Vec<i64>,
    bid_i: Vec<i64>,
    ask_i: Vec<i64>,
    bid_qty_i: Vec<i64>,
    ask_qty_i: Vec<i64>,
    aggressor: Vec<String>,
    trade_id: Vec<String>,
    source_id: Vec<String>,
    symbol: Vec<String>,
    fidelity: Vec<String>,
}

impl Columns {
    fn len(&self) -> usize {
        self.event_type.len()
    }
    fn is_empty(&self) -> bool {
        self.event_type.is_empty()
    }
    fn clear(&mut self) {
        *self = Self::default();
    }
    fn to_batch(&self, schema: &Schema) -> Result<RecordBatch> {
        let b_str = |values: &Vec<String>| -> Result<Arc<dyn arrow::array::Array>> {
            let mut builder = StringBuilder::new();
            for v in values {
                builder.append_value(v);
            }
            Ok(Arc::new(builder.finish()))
        };
        let b_u64 = |values: &Vec<u64>| -> Arc<dyn arrow::array::Array> {
            let mut builder = UInt64Builder::new();
            for v in values {
                builder.append_value(*v);
            }
            Arc::new(builder.finish())
        };
        let b_i64 = |values: &Vec<i64>| -> Arc<dyn arrow::array::Array> {
            let mut builder = Int64Builder::new();
            for v in values {
                builder.append_value(*v);
            }
            Arc::new(builder.finish())
        };
        Ok(RecordBatch::try_new(
            Arc::new(schema.clone()),
            vec![
                b_str(&self.event_type)?,
                b_u64(&self.ts_ns),
                b_u64(&self.recv_ns),
                b_i64(&self.source_ordinal),
                b_i64(&self.price_i),
                b_i64(&self.qty_i),
                b_i64(&self.bid_i),
                b_i64(&self.ask_i),
                b_i64(&self.bid_qty_i),
                b_i64(&self.ask_qty_i),
                b_str(&self.aggressor)?,
                b_str(&self.trade_id)?,
                b_str(&self.source_id)?,
                b_str(&self.symbol)?,
                b_str(&self.fidelity)?,
            ],
        )
        .map_err(|e| anyhow!("ARROW_BATCH:{e}"))?)
    }
}

struct Emitter {
    writer: ArrowWriter<std::fs::File>,
    schema: Schema,
    symbol: String,
    batch_rows: usize,
    columns: Columns,
    ordinal: i64,
    counts: BTreeMap<String, i64>,
    first_ts: Option<u64>,
    last_ts: Option<u64>,
}

impl Emitter {
    fn flush(&mut self) -> Result<()> {
        if self.columns.is_empty() {
            return Ok(());
        }
        let batch = self.columns.to_batch(&self.schema)?;
        self.writer
            .write(&batch)
            .map_err(|e| anyhow!("PARQUET_WRITE:{e}"))?;
        self.columns.clear();
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    fn emit(
        &mut self,
        kind: &str,
        ts_ns: u64,
        price_i: i64,
        qty_i: i64,
        bid_i: i64,
        ask_i: i64,
        bid_qty_i: i64,
        ask_qty_i: i64,
        aggressor: &str,
        trade_id: &str,
    ) -> Result<()> {
        if let Some(last) = self.last_ts {
            if ts_ns < last {
                bail!("EVENT_TS_REGRESSION");
            }
        }
        let fidelity = match kind {
            "Quote" => FIDELITY_QUOTE,
            "Trade" => FIDELITY_TRADE,
            "Mark" => FIDELITY_MARK,
            "Funding" => FIDELITY_FUNDING,
            other => bail!("UNKNOWN_KIND:{other}"),
        };
        self.columns.event_type.push(kind.to_string());
        self.columns.ts_ns.push(ts_ns);
        self.columns.recv_ns.push(ts_ns);
        self.columns.source_ordinal.push(self.ordinal);
        self.columns.price_i.push(price_i);
        self.columns.qty_i.push(qty_i);
        self.columns.bid_i.push(bid_i);
        self.columns.ask_i.push(ask_i);
        self.columns.bid_qty_i.push(bid_qty_i);
        self.columns.ask_qty_i.push(ask_qty_i);
        self.columns.aggressor.push(aggressor.to_string());
        self.columns.trade_id.push(trade_id.to_string());
        self.columns.source_id.push(SOURCE_ID.to_string());
        self.columns.symbol.push(self.symbol.clone());
        self.columns.fidelity.push(fidelity.to_string());
        *self.counts.entry(kind.to_string()).or_insert(0) += 1;
        if self.first_ts.is_none() {
            self.first_ts = Some(ts_ns);
        }
        self.last_ts = Some(ts_ns);
        self.ordinal += 1;
        if self.columns.len() >= self.batch_rows {
            self.flush()?;
        }
        Ok(())
    }
}

/// Convert one UTC archive day to typed Parquet with bounded batches.
/// Returns the manifest (also written as `<out>.json`).
pub fn convert_archive_day(cfg: &ConvertConfig) -> Result<Value> {
    if cfg.day.as_str() >= SEALED_START {
        bail!("SEALED_RESERVE_REFUSED:{}", cfg.day);
    }
    if cfg.batch_rows == 0 || cfg.batch_rows > 1_000_000 {
        bail!("INVALID_BATCH_ROWS");
    }
    let start_ms = parse_day_start_ms(&cfg.day)?;
    let end_ms = start_ms
        .checked_add(86_400_000)
        .ok_or_else(|| anyhow!("INVALID_DAY:{}", cfg.day))?;
    for p in [&cfg.agg_zip, &cfg.mark_zip, &cfg.funding_zip] {
        if !p.is_file() {
            bail!("ZIP_MISSING:{}", p.display());
        }
    }
    let source_hashes = json!({
        "agg_trades": sha256_file(&cfg.agg_zip)?,
        "mark_1m": sha256_file(&cfg.mark_zip)?,
        "funding": sha256_file(&cfg.funding_zip)?,
    });

    let mark_rows = load_marks(&cfg.mark_zip)?;
    let funding_all = load_funding(&cfg.funding_zip)?;
    let fund_rows: Vec<(i64, i64)> = funding_all
        .into_iter()
        .filter(|(ts, _)| *ts >= start_ms && *ts < end_ms)
        .collect();

    let mut pending: Vec<Pending> = Vec::new();
    for (ts_ms, price_i) in mark_rows {
        pending.push(Pending {
            ts_ms,
            order: 0,
            kind: PendingKind::Mark,
            coeff: price_i,
        });
    }
    for (ts_ms, rate_i) in fund_rows {
        pending.push(Pending {
            ts_ms,
            order: 1,
            kind: PendingKind::Funding,
            coeff: rate_i,
        });
    }
    pending.sort_by_key(|p| (p.ts_ms, p.order));
    let mut pending_idx = 0usize;

    if let Some(parent) = cfg.out_path.parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent)?;
        }
    }
    let tmp_path: PathBuf = {
        let mut name = cfg
            .out_path
            .file_name()
            .map(|n| n.to_os_string())
            .unwrap_or_default();
        name.push(".tmp");
        cfg.out_path
            .parent()
            .map(|p| p.join(&name))
            .unwrap_or_else(|| PathBuf::from(&name))
    };
    let tmp_file = File::create(&tmp_path)
        .map_err(|e| anyhow!("OUTPUT_UNWRITABLE:{}: {e}", tmp_path.display()))?;
    let schema = parquet_schema();
    let props = WriterProperties::builder()
        .set_compression(parquet::basic::Compression::ZSTD(
            parquet::basic::ZstdLevel::default(),
        ))
        .set_dictionary_enabled(true)
        .build();
    let writer = ArrowWriter::try_new(tmp_file, Arc::new(schema.clone()), Some(props))
        .map_err(|e| anyhow!("PARQUET_WRITER:{e}"))?;
    let mut emitter = Emitter {
        writer,
        schema: schema.clone(),
        symbol: cfg.symbol.clone(),
        batch_rows: cfg.batch_rows,
        columns: Columns::default(),
        ordinal: 0,
        counts: BTreeMap::new(),
        first_ts: None,
        last_ts: None,
    };

    // Local helper as a macro-free closure is awkward with &mut emitter;
    // flush pending entries with ts_ms < limit in canonical order.
    macro_rules! flush_before {
        ($limit_ms:expr) => {{
            let limit_ms: i64 = $limit_ms;
            while pending_idx < pending.len() && pending[pending_idx].ts_ms < limit_ms {
                let p = &pending[pending_idx];
                let ts_ns = ts_ms_to_ns(p.ts_ms)?;
                match p.kind {
                    PendingKind::Mark => {
                        emitter.emit("Mark", ts_ns, p.coeff, 0, 0, 0, 0, 0, "", "")?
                    }
                    PendingKind::Funding => {
                        emitter.emit("Funding", ts_ns, p.coeff, 0, 0, 0, 0, 0, "", "")?
                    }
                }
                pending_idx += 1;
            }
        }};
    }

    let conversion: Result<()> = (|| {
        let mut archive = open_zip_archive(&cfg.agg_zip)?;
        let (mut reader, index) = zip_csv_reader(
            &mut archive,
            &cfg.agg_zip,
            &["transact_time", "price", "quantity", "is_buyer_maker", "agg_trade_id"],
        )?;
        let mut last_archive_ts_ms: Option<i64> = None;
        let mut last_second: Option<i64> = None;
        for result in reader.records() {
            let record = result.map_err(|e| anyhow!("CSV_RECORD_INVALID:{e}"))?;
            let ts_ms = parse_ts_ms(field(&record, &index, "transact_time")?, "TRADE")?;
            if let Some(prev) = last_archive_ts_ms {
                if ts_ms < prev {
                    bail!("ARCHIVE_TS_REGRESSION");
                }
            }
            last_archive_ts_ms = Some(ts_ms);
            let price_i = coeff(field(&record, &index, "price")?, "trade.price")?;
            let bucket = ts_ms.div_euclid(1000);
            if last_second != Some(bucket) {
                last_second = Some(bucket);
                let quote_ts_ms = bucket * 1000;
                flush_before!(quote_ts_ms);
                let bid_i = price_i
                    .checked_sub(TICK_I)
                    .ok_or_else(|| anyhow!("DECIMAL_OVERFLOW:quote.bid"))?;
                let ask_i = price_i
                    .checked_add(TICK_I)
                    .ok_or_else(|| anyhow!("DECIMAL_OVERFLOW:quote.ask"))?;
                emitter.emit(
                    "Quote",
                    ts_ms_to_ns(quote_ts_ms)?,
                    0,
                    0,
                    bid_i,
                    ask_i,
                    QUOTE_QTY_I,
                    QUOTE_QTY_I,
                    "",
                    "",
                )?;
            }
            flush_before!(ts_ms);
            let qty_i = qty_coeff(field(&record, &index, "quantity")?, "trade.quantity")?;
            let aggressor = if field(&record, &index, "is_buyer_maker")? == "true" {
                "SELL"
            } else {
                "BUY"
            };
            let trade_id = field(&record, &index, "agg_trade_id")?.to_string();
            emitter.emit(
                "Trade",
                ts_ms_to_ns(ts_ms)?,
                price_i,
                qty_i,
                0,
                0,
                0,
                0,
                aggressor,
                &trade_id,
            )?;
        }
        while pending_idx < pending.len() {
            let p = &pending[pending_idx];
            let ts_ns = ts_ms_to_ns(p.ts_ms)?;
            match p.kind {
                PendingKind::Mark => emitter.emit("Mark", ts_ns, p.coeff, 0, 0, 0, 0, 0, "", "")?,
                PendingKind::Funding => {
                    emitter.emit("Funding", ts_ns, p.coeff, 0, 0, 0, 0, 0, "", "")?
                }
            }
            pending_idx += 1;
        }
        emitter.flush()?;
        Ok(())
    })();

    // Always close the writer so the tmp file is complete before rename,
    // even on error (the tmp file is then removed by the caller path).
    let close_result = emitter.writer.close().map_err(|e| anyhow!("PARQUET_CLOSE:{e}"));
    if let Err(e) = conversion {
        let _ = std::fs::remove_file(&tmp_path);
        return Err(e);
    }
    close_result?;
    std::fs::rename(&tmp_path, &cfg.out_path)
        .map_err(|e| anyhow!("OUTPUT_RENAME:{}: {e}", cfg.out_path.display()))?;

    let output_sha = sha256_file(&cfg.out_path)?;
    let num_row_groups = {
        let file = File::open(&cfg.out_path)?;
        let reader = parquet::file::reader::SerializedFileReader::new(file)
            .map_err(|e| anyhow!("PARQUET_READ:{e}"))?;
        reader.metadata().num_row_groups() as i64
    };
    let counts_json: serde_json::Map<String, Value> = emitter
        .counts
        .iter()
        .map(|(k, v)| (k.clone(), json!(*v)))
        .collect();
    let manifest = json!({
        "schema_version": "2.0.0",
        "day": cfg.day,
        "symbol": cfg.symbol,
        "source": SOURCE_ID,
        "source_hashes": source_hashes,
        "parquet_path": cfg.out_path.to_string_lossy(),
        "parquet_sha256": output_sha,
        "canonical_ndjson_sha256": Value::Null,
        "canonical_ndjson_sha256_note": "compute via python qel.prepare.archive_parquet.canonical_ndjson_sha on the parquet file; byte parity with the legacy tape is verified by scripts/verify_parquet_parity.py",
        "counts": counts_json,
        "rows": emitter.ordinal,
        "num_row_groups": num_row_groups,
        "time_range": match (emitter.first_ts, emitter.last_ts) {
            (Some(first), Some(last)) => json!({"start_ns": first.to_string(), "end_ns": last.to_string()}),
            _ => json!({}),
        },
        "fidelity": {
            "BOOK_COVERAGE": FIDELITY_QUOTE,
            "PRICE_PATH": FIDELITY_TRADE,
            "FEED_LATENCY": "assumed_zero (recv_ns == ts_ns)",
            "FUNDING": "observed_venue_funding (monthly fundingRate)",
        },
        "ordering": "source_ordinal is canonical emission order; equal ts_ns preserves Quote/Trade then Mark then Funding",
        "scale": SCALE,
        "sealed_start": SEALED_START,
        "engine": "rust qel-ingest (zip -> csv -> checked fixed-point -> parquet)",
    });
    let manifest_path = cfg.out_path.with_extension("json");
    let mut manifest_file = File::create(&manifest_path)?;
    manifest_file.write_all(serde_json::to_string_pretty(&manifest)?.as_bytes())?;
    manifest_file.write_all(b"\n")?;
    Ok(manifest)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn write_zip(path: &Path, csv_text: &str) -> Result<()> {
        let file = File::create(path)?;
        let mut zip = zip::ZipWriter::new(file);
        let options = zip::write::FileOptions::<()>::default()
            .compression_method(zip::CompressionMethod::Stored);
        zip.start_file("data.csv", options)?;
        zip.write_all(csv_text.as_bytes())?;
        zip.finish()?;
        Ok(())
    }

    fn fixture_dir(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "qel-ingest-test-{}-{}",
            name,
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    const AGG: &str = "agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker\n1,100.00,0.100,1,1,1725148800000,false\n2,100.10,0.200,2,2,1725148800500,true\n3,100.20,0.300,3,3,1725148801500,false\n";
    const MARK: &str = "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore\n1725148800000,100.00,100.20,99.90,100.20,0,1725148859999,0,60,0,0,0\n";
    const FUNDING: &str = "calc_time,funding_interval_hours,last_funding_rate\n1725148800000,8,0.00010000\n1725177600000,8,-1E-8\n";

    fn convert_fixture(dir: &Path, agg: &str, day: &str) -> Result<(ConvertConfig, Value)> {
        convert_fixture_mark(dir, agg, MARK, day)
    }

    fn convert_fixture_mark(dir: &Path, agg: &str, mark: &str, day: &str) -> Result<(ConvertConfig, Value)> {
        let agg_zip = dir.join("agg.zip");
        let mark_zip = dir.join("mark.zip");
        let funding_zip = dir.join("funding.zip");
        write_zip(&agg_zip, agg)?;
        write_zip(&mark_zip, mark)?;
        write_zip(&funding_zip, FUNDING)?;
        let out = dir.join("out.parquet");
        let cfg = ConvertConfig {
            agg_zip,
            mark_zip,
            funding_zip,
            out_path: out,
            day: day.to_string(),
            symbol: "BTCUSDT".to_string(),
            batch_rows: 4,
        };
        let manifest = convert_archive_day(&cfg)?;
        Ok((cfg, manifest))
    }

    #[test]
    fn day_math() {
        assert_eq!(parse_day_start_ms("1970-01-01").unwrap(), 0);
        assert_eq!(
            parse_day_start_ms("2024-09-01").unwrap(),
            1725148800000i64
        );
        assert!(parse_day_start_ms("2024-02-31").is_err());
        assert!(parse_day_start_ms("2024-13-01").is_err());
        assert!(parse_day_start_ms("20240901").is_err());
    }

    #[test]
    fn converts_fixture_in_canonical_order() {
        let dir = fixture_dir("order");
        let (cfg, manifest) = convert_fixture(&dir, AGG, "2024-09-01").unwrap();
        // Q,T (first trade), Funding (ts == day start, flushed before the
        // second trade whose ts is strictly later), T (first second), Q,T
        // (second second), Mark, Funding (08:00, flushed at the end).
        assert_eq!(manifest["rows"], json!(8));
        assert_eq!(manifest["counts"]["Quote"], json!(2));
        assert_eq!(manifest["counts"]["Trade"], json!(3));
        assert_eq!(manifest["counts"]["Mark"], json!(1));
        // Both funding rows fall inside the day window.
        assert_eq!(manifest["counts"]["Funding"], json!(2));
        // Read back with the parquet arrow reader and check order/coeffs.
        let batches = read_all_batches(&cfg.out_path);
        let kinds: Vec<String> = batches
            .iter()
            .flat_map(|b| column_strings(b, "event_type"))
            .collect();
        let expected: Vec<String> = ["Quote", "Trade", "Funding", "Trade", "Quote", "Trade", "Mark", "Funding"]
            .iter()
            .map(|s| s.to_string())
            .collect();
        assert_eq!(kinds, expected);
        let _ = dir;
    }

    #[test]
    fn rejects_trade_kline_mislabelled_as_mark() {
        let dir = fixture_dir("mark-source");
        let trade_mark = "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore\n1725148800000,100.00,100.20,99.90,100.20,1.0,1725148859999,100.0,3,0.6,60.0,0\n";
        let result = convert_fixture_mark(&dir, AGG, trade_mark, "2024-09-01");
        assert!(result.is_err(), "trade kline must be rejected as mark");
        let err = result.err().unwrap();
        assert!(err.to_string().contains("MARK_SOURCE_MISSING"), "{err}");
        let _ = dir;
    }

    #[test]
    fn rejects_sealed_and_bad_inputs() {
        let dir = fixture_dir("reject");
        assert!(convert_fixture(&dir, AGG, "2026-03-08").is_err());
        // Price with 9dp precision must fail closed.
        let bad = "agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker\n1,100.000000001,0.100,1,1,1725148800000,false\n";
        assert!(convert_fixture(&dir, bad, "2024-09-01").is_err());
        // Out-of-order transact_time must fail closed.
        let ooo = "agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker\n1,100.00,0.100,1,1,1725148801000,false\n2,100.10,0.200,2,2,1725148800500,true\n";
        assert!(convert_fixture(&dir, ooo, "2024-09-01").is_err());
    }

    fn read_all_batches(path: &Path) -> Vec<RecordBatch> {
        use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
        let file = File::open(path).unwrap();
        let builder = ParquetRecordBatchReaderBuilder::try_new(file).unwrap();
        builder
            .with_batch_size(64)
            .build()
            .unwrap()
            .map(|r| r.unwrap())
            .collect()
    }

    fn column_strings(batch: &RecordBatch, name: &str) -> Vec<String> {
        use arrow::array::{Array as _, StringArray};
        let idx = batch.schema().index_of(name).unwrap();
        let arr = batch
            .column(idx)
            .as_any()
            .downcast_ref::<StringArray>()
            .unwrap();
        (0..arr.len()).map(|i| arr.value(i).to_string()).collect()
    }
}
