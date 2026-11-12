//! `qel-ingest`: Binance archive ZIP -> typed Parquet (schema v2).
//!
//! Usage:
//!   qel-ingest --agg <aggTrades.zip> --mark <1m.zip> \
//!     --funding <fundingRate-YYYY-MM.zip> --day 2024-09-01 \
//!     --out <out.parquet> [--symbol BTCUSDT] [--batch-rows 65536]
//!
//! Prints the manifest JSON to stdout. Fails closed (non-zero exit) on
//! sealed days, missing/invalid inputs, precision/overflow violations,
//! or timestamp regressions.

use std::path::PathBuf;

use anyhow::{anyhow, bail, Result};
use qel_data_engine_ingest::{convert_archive_day, ConvertConfig, DEFAULT_BATCH_ROWS};

fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().collect();
    let mut cfg = ConvertConfig {
        symbol: "BTCUSDT".to_string(),
        batch_rows: DEFAULT_BATCH_ROWS,
        ..Default::default()
    };
    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--agg" => {
                cfg.agg_zip = PathBuf::from(args.get(i + 1).ok_or_else(|| anyhow!("--agg value"))?);
                i += 2;
            }
            "--mark" => {
                cfg.mark_zip = PathBuf::from(args.get(i + 1).ok_or_else(|| anyhow!("--mark value"))?);
                i += 2;
            }
            "--funding" => {
                cfg.funding_zip =
                    PathBuf::from(args.get(i + 1).ok_or_else(|| anyhow!("--funding value"))?);
                i += 2;
            }
            "--out" => {
                cfg.out_path = PathBuf::from(args.get(i + 1).ok_or_else(|| anyhow!("--out value"))?);
                i += 2;
            }
            "--day" => {
                cfg.day = args.get(i + 1).ok_or_else(|| anyhow!("--day value"))?.clone();
                i += 2;
            }
            "--symbol" => {
                cfg.symbol = args.get(i + 1).ok_or_else(|| anyhow!("--symbol value"))?.clone();
                i += 2;
            }
            "--batch-rows" => {
                cfg.batch_rows = args
                    .get(i + 1)
                    .ok_or_else(|| anyhow!("--batch-rows value"))?
                    .parse()?;
                i += 2;
            }
            other => bail!("unknown argument {other}"),
        }
    }
    if cfg.agg_zip.as_os_str().is_empty()
        || cfg.mark_zip.as_os_str().is_empty()
        || cfg.funding_zip.as_os_str().is_empty()
        || cfg.out_path.as_os_str().is_empty()
        || cfg.day.is_empty()
    {
        bail!("--agg, --mark, --funding, --day and --out are required");
    }
    let manifest = convert_archive_day(&cfg)?;
    println!("{}", serde_json::to_string_pretty(&manifest)?);
    Ok(())
}
