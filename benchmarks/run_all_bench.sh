#!/usr/bin/env bash
# Run B0, B1 and B5 once per declared resource profile (RP-4G, RP-8G).
# Each process runs in a cgroup v2 scope with MemoryMax set and swap disabled.
set -euo pipefail

ROOT=${QEL_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}
CACHE=${QEL_BUILD_CACHE:-$ROOT/.build-cache}
BIN=${QEL_BENCH_BIN:-$CACHE/qel-bench}
B0_BIN=${QEL_B0_BIN:-$CACHE/qel-policy-target/release/bench_b0}
RUNNER=$ROOT/benchmarks/run_profile.sh
RAW=$ROOT/evidence/labd/raw
SCRATCH=${QEL_BENCH_SCRATCH:-$CACHE}
mkdir -p "$RAW" "$SCRATCH"

run_profile() {
  local profile=$1 mem=$2 shift_by=2
  shift $shift_by
  local unit="qel-bench-$(echo "$1" | tr '[:upper:]' '[:lower:]')-${profile}"
  "$RUNNER" "$unit" "$mem" "$RAW/$1_${profile}.peak" -- "${@:2}"
}

for profile_mem in "RP-4G:4G" "RP-8G:8G"; do
  profile=${profile_mem%%:*}
  mem=${profile_mem##*:}

  echo "=== $profile B0 native"
  "$RUNNER" "qel-b0-${profile,,}" "$mem" "$RAW/B0_NATIVE_${profile}.peak" -- \
    "$B0_BIN" --policy "$ROOT/benchmarks/corpus/b0_policy_256.json" --mode native \
    --repeats 12 --warmups 2 --out "$RAW/B0_NATIVE_${profile}.json"

  echo "=== $profile B0 schema-only"
  "$RUNNER" "qel-b0s-${profile,,}" "$mem" "$RAW/B0_SCHEMA_${profile}.peak" -- \
    "$B0_BIN" --policy "$ROOT/benchmarks/corpus/b0_policy_256.json" --mode schema-only \
    --repeats 12 --warmups 2 --out "$RAW/B0_SCHEMA_${profile}.json"

  echo "=== $profile B1"
  mkdir -p "$SCRATCH/b1-out-${profile}"
  "$RUNNER" "qel-b1-${profile,,}" "$mem" "$RAW/B1_${profile}.peak" -- \
    "$BIN" b1 --corpus "$ROOT/benchmarks/corpus/b1_bars.csv" \
    --workload "$ROOT/benchmarks/corpus/b1_workload.json" \
    --out-dir "$SCRATCH/b1-out-${profile}" \
    --repeats 12 --warmups 2 --out "$RAW/B1_${profile}.json"

  echo "=== $profile B5"
  rm -rf "$SCRATCH/b5-${profile}"
  mkdir -p "$SCRATCH/b5-${profile}"
  "$RUNNER" "qel-b5-${profile,,}" "$mem" "$RAW/B5_${profile}.peak" -- \
    "$BIN" b5 --events "$ROOT/benchmarks/corpus/b5_events.jsonl" \
    --artifacts "$ROOT/benchmarks/corpus/b5_artifacts.json" \
    --work-dir "$SCRATCH/b5-${profile}" \
    --repeats 12 --warmups 2 --crash-trials 6 --out "$RAW/B5_${profile}.json"
done
echo "ALL_PROFILES_DONE"
