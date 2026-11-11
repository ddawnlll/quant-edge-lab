//! `qel-parquet-runner`: Parquet -> Arrow RecordBatch -> Nautilus replay.
//!
//! Production path: Parquet is read as Arrow RecordBatches, converted
//! to typed `Vec<Data>` via `qel-data-engine-nautilus-adapter`, and
//! fed to the same engine/strategy/accounting code as the legacy
//! NDJSON runner (`qel_native_runner::run_parquet`). No `to_pylist()`,
//! no JSONL intermediate, no per-event JSON parsing.
//!
//! CLI mirrors `qel-native-runner` with `--parquet` instead of
//! `--events`, plus `--batch-rows` for the Arrow read batch size.

fn main() -> anyhow::Result<()> {
    qel_native_runner::run_parquet()
}
