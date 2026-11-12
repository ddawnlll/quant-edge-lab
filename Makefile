# QEL local build and verification entry points.
PY := .venv/bin/python
export PYTHONPATH := python
export QEL_ROOT := $(CURDIR)

# Build caches default to a repo-local directory so a fresh checkout builds on
# any host. Set QEL_BUILD_CACHE (or CARGO_TARGET_DIR) to reuse an external cache.
QEL_BUILD_CACHE ?= $(CURDIR)/.build-cache
CARGO_TARGET_DIR ?= $(QEL_BUILD_CACHE)/qel-native
CONTRACT_TARGET_DIR ?= $(QEL_BUILD_CACHE)/qel-contract-rs
GO ?= go

.PHONY: setup contracts test fixtures mutations crosslang evidence native-spike clean

setup:
	python3 -m venv .venv
	.venv/bin/pip install -q --upgrade pip
	.venv/bin/pip install -q jsonschema numpy scipy pyarrow duckdb pytest

contracts:
	mkdir -p bin
	$(GO) build -o bin/qel-contract ./contracts/go/cmd/qel-contract
	cd contracts/rust && CARGO_TARGET_DIR=$(CONTRACT_TARGET_DIR) cargo build --release
	cp $(CONTRACT_TARGET_DIR)/release/qel-contract bin/qel-contract-rs

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
