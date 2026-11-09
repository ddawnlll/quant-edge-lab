# TASK-25 benchmark harness

Frozen corpora, benchmark drivers and evidence assembly for the TASK-25
checks `B0`, `B1`, `B5` and `T-RELEASE`.

## Layout

| Path | Purpose |
| --- | --- |
| `gen_corpus.py` | Deterministic corpus generator and manifest (`--check` verifies without writing) |
| `corpus/MANIFEST.json` | Frozen SHA-256 of every corpus file and the declared counts |
| `run_profile.sh` | Runs one command in a cgroup v2 scope with `MemoryMax` and `MemorySwapMax=0` |
| `run_all_bench.sh` | Runs B0/B1/B5 once per declared profile (`RP-4G`, `RP-8G`) |
| `assemble_evidence.py` | Merges raw runs into `evidence/labd/B*.json` and `T25_ACCEPTANCE.json` |
| `build_evidence.py` | Measures T-RELEASE builds, lock hashes, parity and correctness suites |
| `../policy/rust/src/bin/bench_b0.rs` | B0 typed-specification driver (PARSE/SCHEMA/TYPECHECK/HASH) |
| `../cmd/qel-bench` | B1 batch screen and B5 ledger/restore drivers |

## Corpora (frozen before any timed run)

| Check | File | Frozen content |
| --- | --- | --- |
| B0 | `b0_policy_256.json` | 256-node DAG, OPERATOR_SET_1_1, 25 stateful operators, 4 boolean condition nodes |
| B1 | `b1_bars.csv` | 1,000,000 integer sawtooth bars, 60 s interval, `close=100000+(i%1000)`, `open=previous_close` |
| B1 | `b1_workload.json` | 8 variants, thresholds 1.0..2.75, 8 common operators, feature plan |
| B5 | `b5_events.jsonl` | 100,000 ledger events (decisions, PASS records, artifact pointers, job states) |
| B5 | `b5_artifacts.json` | 1,000 artifact pointer declarations |

`python3 benchmarks/gen_corpus.py --check` exits non-zero if the bytes drift.

## Measurements and boundaries

- B0 measures `PARSE + SCHEMA + TYPECHECK + HASH` on each repeat from the raw
  bytes. `schema-only` (document and registry checks) and `native`
  (full `parse_strategy` plus engine state construction) are timed and reported
  separately. The policy evaluator is interpreted, so native/JIT compilations
  are structurally `0`; the counter is explicit and reported.
- B1 includes `READ + FEATURES + VARIANTS + OUTPUT_HASH_FSYNC`. The output hash
  and `fsync` are inside the timed region; `phase_p50_ms.output_hash_fsync`
  reports that share. Variants additionally require `er > 0` so the eighth
  common operator participates in the decisions.
- B5 appends the 100,000 events in durable batches (one SQLite transaction with
  `synchronous=FULL` per batch), seals 1,000 artifacts, then verifies the full
  hash chain, measures a full-ledger read transaction, performs a
  `VACUUM INTO` backup plus reopen/verify restore, and runs the F17 SIGKILL
  crash matrix with acknowledgement files.
- Two warmups are discarded; at least ten timed repeats are kept. `failed_attempts`
  is retained in every report and any failure prevents a PASS verdict.
- Corpus hashes are computed before timing and re-hashed inside each driver.
  B1/B5 outputs are hashed with SHA-256 and fsynced to disk.

## Declared profiles

`RP-4G` = 4096 MiB, `RP-8G` = 8192 MiB. Both are enforced with a transient
systemd **user** scope: `MemoryMax=<limit>`, `MemorySwapMax=0`, and
`memory.peak` is read from the same cgroup after the process exits. This host
does not delegate a writable cgroup root to the login session, so `sudo` or
direct cgroup writes are unavailable. If user scopes are unavailable the run
fails rather than falling back silently; no 4/8 GiB result is claimed without
the limit being applied.

## Reproduce

```bash
python3 benchmarks/gen_corpus.py --check
cd policy/rust && cargo build --release --bin bench_b0
go build -o /home/quant/quant/.build-cache/qel-bench ./cmd/qel-bench
benchmarks/run_all_bench.sh
python3 benchmarks/assemble_evidence.py
python3 benchmarks/build_evidence.py
```

Raw per-profile reports and `memory.peak` values are kept under
`evidence/labd/raw/`.

## Acceptance boundary

B0/B1/B5 and the benchmark-scoped T-RELEASE evidence are measured here. Full
TASK-25 acceptance also requires the B2/B3 checks under both enforced profiles
and the B4 100M-event plateau. B4 100M remains `BLOCKED_NOT_RUN` from TASK-19,
so `evidence/labd/T25_ACCEPTANCE.json` records
`PARTIAL/EVIDENCE_PENDING` with the exact missing items and does not claim full
acceptance.
