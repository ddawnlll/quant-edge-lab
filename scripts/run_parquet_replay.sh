#!/usr/bin/env bash
# Reproducible real-data differential replay for 2024-09-01 (PERF v1.2 integration).
#
# Pipeline:
#   A. Legacy:  data/native/_fold/2024-09-01.jsonl (canonical NDJSON)
#               -> qel-native-runner (rebuilt, hash-pinned below)
#   B. New:     Binance ZIPs -> qel-ingest (Rust) -> Parquet
#               -> qel-parquet-runner (Arrow RecordBatch -> Vec<Data>)
#               -> same Nautilus engine/strategy as A
# Comparison: scripts/verify_parquet_parity.py (Parquet vs NDJSON sha)
#             scripts/compare_replay_outputs.py (tape + accounting)
#
# Prerequisites: cargo, python venv at .venv (pyarrow), data present.
# Builds use shared cargo cache, -j2, nice -n 10. Never cargo clean.
# Full A3/B6 runs are NOT launched by this script.
set -euo pipefail
cd "$(dirname "$0")/.."

DAY="${DAY:-2024-09-01}"
AGG="data/raw/ARCHIVE_BINANCE_USDM/BTCUSDT/${DAY}/BTCUSDT-aggTrades-${DAY}.zip"
MARK="data/raw/ARCHIVE_BINANCE_USDM/BTCUSDT/${DAY}/BTCUSDT-1m-${DAY}.zip"
FUNDING="data/raw/ARCHIVE_BINANCE_USDM/BTCUSDT/funding/BTCUSDT-fundingRate-2024-09.zip"
LEGACY="data/native/_fold/${DAY}.jsonl"
STRATEGY="data/native/pilot-m2fold-20261111/out/reports/STRAT-VAR-10.strategy.json"
OUTDIR="${OUTDIR:-data/native/_integration/${DAY}}"
mkdir -p "$OUTDIR"
PARQUET="${OUTDIR}/${DAY}.parquet"
LEGACY_OUT="${OUTDIR}/legacy.json"
PARQUET_OUT="${OUTDIR}/parquet.json"
CHUNK="${CHUNK:-65536}"

echo "== build (shared cache, -j2, nice) =="
nice -n 10 cargo build -j2 --manifest-path data-engine/Cargo.toml -p qel-data-engine-ingest
nice -n 10 cargo build -j2 --manifest-path native/runner/Cargo.toml --bins

INGEST="data-engine/target/debug/qel-ingest"
LEGACY_BIN="native/runner/target/debug/qel-native-runner"
PARQUET_BIN="native/runner/target/debug/qel-parquet-runner"
sha256sum "$INGEST" "$LEGACY_BIN" "$PARQUET_BIN" | tee "${OUTDIR}/binary-sha256.txt"

echo "== B1: Rust ZIP -> Parquet =="
time "$INGEST" --agg "$AGG" --mark "$MARK" --funding "$FUNDING" \
  --day "$DAY" --out "$PARQUET" | tee "${OUTDIR}/ingest-manifest.json"
ls -lh "$PARQUET"

echo "== B2: Parquet vs legacy NDJSON parity =="
PYTHONPATH=python .venv/bin/python scripts/verify_parquet_parity.py \
  --parquet "$PARQUET" --ndjson "$LEGACY" --out "${OUTDIR}/parity.json"

echo "== A: legacy NDJSON -> Nautilus =="
time "$LEGACY_BIN" --events "$LEGACY" --strategy "$STRATEGY" \
  --out "$LEGACY_OUT" --chunk "$CHUNK"

echo "== B: Parquet -> Arrow -> Nautilus =="
time "$PARQUET_BIN" --parquet "$PARQUET" --strategy "$STRATEGY" \
  --out "$PARQUET_OUT" --chunk "$CHUNK" --batch-rows 65536

echo "== differential comparison =="
PYTHONPATH=python .venv/bin/python scripts/compare_replay_outputs.py \
  --legacy "$LEGACY_OUT" --parquet "$PARQUET_OUT" --out "${OUTDIR}/differential.json"
echo "DONE: see ${OUTDIR}/differential.json"
