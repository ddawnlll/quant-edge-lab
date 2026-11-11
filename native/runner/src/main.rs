//! `qel-native-runner`: legacy NDJSON -> Nautilus replay.
//!
//! The implementation lives in the `qel_native_runner` library crate
//! (`src/lib.rs`) so the Parquet runner shares the same strategy,
//! engine setup and accounting code. This binary preserves the
//! historical CLI and output schema exactly.

fn main() -> anyhow::Result<()> {
    qel_native_runner::run_ndjson_legacy()
}
