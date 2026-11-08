# Resume

Updated: 2026-11-08T21:15Z. Repository: `/home/quant/quant/quant-edge-lab`.

## Verified now

- Spec ZIP SHA-256 `db18e7a6...fd30`; `MANIFEST.sha256` OK; baseline
  monograph SHA-256 `71628e4b...d06`; spec validator
  `PASS_SPECIFICATION_ONLY` (evidence/contract/SPEC_VALIDATION.json).
- TASK-01: canonical JCS/decimals/schema validation in Python, Go and Rust.
  Cross-language run-key and canonical bytes agree.
- TASK-02: QEL_FIXTURE_DSL_1 decoder, 44/44 scenarios across F01-F35,
  5/5 mutation controls caught.

## Next exact action

Finish TASK-08: wait for `cargo build --release` in `native/spike`, run the
pure-Rust spike, write `locks/RUNTIME_LOCK.json` with the pinned commit,
toolchain versions and source hashes, and record the capability verdict.
If the LocalView hooks are absent, mark `CAP-LOCAL-VIEW` and `CAP-L2-QUEUE`
as `BLOCKED_BACKEND` with the exact missing API; do not weaken the
requirement.

Then start TASK-03 (Go + bundled SQLite ledger) and TASK-04 (queue/control
protocol), followed by TASK-05/06/07/09/11.

## Commands

```sh
cd /home/quant/quant/quant-edge-lab
export PYTHONPATH=python QEL_ROOT=$PWD
make contracts
make test
make evidence
cd native/spike && CARGO_TARGET_DIR=/home/quant/quant/.build-cache/qel-native cargo build --release
```

## Environment notes

- Go 1.27.3, Rust 1.99.1, Python 3.13.5, 6 CPUs, 11.7 GiB RAM.
- System Python SQLite is 3.46.1 (too old for the spec WAL fix). Use Go
  `modernc.org/sqlite` (bundled) and record its actual `sqlite_version()`.
- No passwordless sudo. User systemd with linger is available.
- GitHub auth available for `ddawnlll`; public repo not created yet.
