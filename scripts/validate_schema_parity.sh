#!/usr/bin/env bash
# Verify Python, Go and Rust agree on valid/invalid StrategySpec documents.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
export PYTHONPATH="$ROOT/python"
valid=specification/examples/strategy_mr.json
invalid=/home/quant/quant/spec-src/bad_strategy.json

for bin in bin/qel-contract bin/qel-contract-rs; do
  "$bin" validate --def StrategySpec --file "$valid"
done
.venv/bin/python - <<'PY'
import json
from qel.contract import validate_def
validate_def("StrategySpec", json.load(open("specification/examples/strategy_mr.json")))
print("VALID StrategySpec")
PY

for bin in bin/qel-contract bin/qel-contract-rs; do
  if "$bin" validate --def StrategySpec --file "$invalid" >/dev/null 2>&1; then
    echo "FAIL $bin accepted invalid strategy" >&2
    exit 1
  fi
done
if .venv/bin/python -c 'import json; from qel.contract import validate_def; validate_def("StrategySpec", json.load(open("/home/quant/quant/spec-src/bad_strategy.json")))' >/dev/null 2>&1; then
  echo "FAIL python accepted invalid strategy" >&2
  exit 1
fi
echo "PASS all three languages reject the invalid strategy"
