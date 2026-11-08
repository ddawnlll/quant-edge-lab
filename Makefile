# QEL local build and verification entry points.
PY := .venv/bin/python
export PYTHONPATH := python
export QEL_ROOT := $(CURDIR)
CARGO_TARGET_DIR ?= /home/quant/quant/.build-cache/qel-native

.PHONY: setup contracts test fixtures mutations crosslang evidence native-spike clean

setup:
	python3 -m venv .venv
	.venv/bin/pip install -q --upgrade pip
	.venv/bin/pip install -q jsonschema numpy scipy pyarrow duckdb pytest

contracts:
	mkdir -p bin
	cd contracts/go && go build -o ../../bin/qel-contract ./cmd/qel-contract
	cd contracts/rust && CARGO_TARGET_DIR=/home/quant/quant/.build-cache/qel-contract-rs cargo build --release
	cp /home/quant/quant/.build-cache/qel-contract-rs/release/qel-contract bin/qel-contract-rs

test:
	$(PY) -m pytest -q tests

fixtures:
	$(PY) -m qel.cli fixtures --out evidence/conformance/FIXTURE_REPORT.json

mutations:
	$(PY) -m qel.cli mutations --out evidence/conformance/MUTATION_REPORT.json

crosslang: contracts
	$(PY) -m qel.cli crosslang --out evidence/contract/CROSSLANG_REPORT.json
	$(PY) -m qel.cli runkey --out evidence/contract/RUNKEY_VECTORS.json

evidence: fixtures mutations crosslang

native-spike:
	cd native/spike && CARGO_TARGET_DIR=$(CARGO_TARGET_DIR) cargo build --release
	$(CARGO_TARGET_DIR)/release/qel-native-spike

clean:
	rm -rf bin .pytest_cache python/**/__pycache__ tests/__pycache__
