#!/usr/bin/env bash
# Consolidated adversarial/revalidation pass.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
export PYTHONPATH="$ROOT/python"
echo "== time =="; date -u +%FT%TZ
echo "== vendored spec tree =="; diff -qr /home/quant/quant/spec-src/quant-edge-lab-v1.1 specification | grep -v '\.spec-venv' || true
echo "== native pin =="; .venv/bin/python scripts/verify_native_pin.py
echo "== cross-language fuzz =="; .venv/bin/python scripts/crosslang_fuzz.py
echo "== schema parity =="; scripts/validate_schema_parity.sh
echo "== python tests =="; .venv/bin/python -m pytest -q
echo "== go tests =="; go test ./...
echo "== rust policy tests =="; (cd policy/rust && cargo test --release)
echo "== fixture mutations =="; .venv/bin/python -m qel.cli mutations --out evidence/conformance/MUTATION_REPORT.json
echo "== provenance =="; .venv/bin/python scripts/validate_provenance.py
echo "== http negatives =="; scripts/validate_http_negative.sh
