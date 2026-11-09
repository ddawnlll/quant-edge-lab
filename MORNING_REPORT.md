# Morning Report

Status time: 2026-11-09T00:10Z. This is a development and verification report.
It is not a profitability claim and does not certify a trading edge.

## Implemented and verified

| Task | Status | Evidence |
| --- | --- | --- |
| Bootstrap | DONE | ZIP SHA-256 `db18e7a6…fd30`, MANIFEST OK, baseline `71628e4b…d06`, spec validator PASS |
| TASK-01 contract SDK | DONE | Python/Go/Rust JCS, decimals, schemas, run keys; cross-language match |
| TASK-02 fixture oracle | DONE | 44/44 scenarios, 10/10 mutation controls caught (expected and input) |
| TASK-03 ledger/artifacts | DONE | crash matrix, orphan/acked checks, backup restore, payload/event-ID/hash-chain verification, blob repair |
| TASK-04 control/queue | DONE | 4-byte framing, peer credentials, leases, idempotency, stale generation |
| TASK-05 public capture | DONE | 20s and 240s live captures; verified daily archive |
| TASK-06 normalize/PIT | PARTIAL | Arrow/Parquet + manifests, raw-segment verification, capture gaps reported; DuckDB unusable on this CPU |
| TASK-07 typed policy | DONE | 26 operators, DAG/type/leak checks, EMA48/STD96 reference, direct operator coverage |
| TASK-08 native feasibility | DONE_WITH_BLOCKED_CAPABILITY | Nautilus v1.231.0 spike and pin re-verified; LocalView/L2 queue blocked |
| TASK-09 independent accounting | DONE | exact tape reconstruction; native residual 0 |
| TASK-11 native taker slice | PARTIAL | 15 orders/8 positions live slice; full archive day 307 orders/154 positions, residual 0; F22 blocked |
| TASK-14 dependent stats | PARTIAL | HAC/bootstrap/IUT/Holm/F27/F28 and four-generator B6 re-run after the bootstrap fix, `JOB-STATS-1` sealed through qeld; full MDE/coverage certificate pending |
| TASK-15/16 evaluator | PARTIAL | E2E frozen cohort + pre-access receipt + Ed25519 verdict verified; OS owner split blocked |
| TASK-20 recovery | DONE | worker killed, lease requeued, generation 2 sealed |
| TASK-21/22 memory/agent | DONE | FTS claims, CAS assignment, stale rejection, rebuild |
| TASK-23 cgroup admission | DONE | real OOM kill, memory/pids/cpu caps, heavy-token and disk admission |
| TASK-24 dashboard | DONE | localhost UI, SSE, health, read-only vs write-token split |
| TASK-26 null calibration | PARTIAL | 4 generators × 1,000 null programs, 0 promotions each; local power grid 0/30 at tested effects |
| TASK-27 soak | RUNNING | 24h sampler started 22:21:50Z; elapsed time not yet satisfied |

## Active services

| Unit | PID | State | Notes |
| --- | --- | --- | --- |
| `qel-qeld.service` | see `systemctl --user show -p MainPID --value qel-qeld` | active | dashboard `http://127.0.0.1:8787/` |
| `qel-worker.service` | see systemd | active | cgroup-limited job worker |
| `qel-soak.service` | see systemd | active | 60s sampler, `runtime/soak/soak.jsonl` |

Dashboard is bound to `127.0.0.1:8787` only. HTTP writes require the
`runtime/http.token` bearer token (mode 0600); the Unix socket uses
`SO_PEERCRED`.

Health: `curl -s http://127.0.0.1:8787/healthz`
Dashboard snapshot: `curl -s http://127.0.0.1:8787/v1/dashboard`
SQLite: 3.54.1 source `2026-10-16 14:45:20 555e07ef…`.

## M3 evaluator acceptance path (observed)

`evidence/labd/EVALUATOR_E2E.json` records a real call sequence through the
sealed component: pre-consumption decision refused, unacknowledged receipt
refused, cohort frozen, alpha `5/200` spent once, decision signed and verified,
second decision refused. Key and DB modes are `0600`. The data is an explicitly
labeled evaluator calibration vector, not a market result.

`JOB-STATS-1` was submitted through the public HTTP job interface, executed by
`qel-worker` under cgroup limits, and reached `OUTPUT_SEALED` with an artifact
reference; `/v1/ledger/verify` returned `{"status":"OK","chain":"verified"}`.

## Ops acceptance path (observed)

`evidence/labd/OPS_E2E.txt` records `qeld` PID 744317 killed, systemd restart to
PID 744722, health `OK`, ledger chain verified, worker and soak active, soak
elapsed 3,309s, and local/remote git equal. `evidence/labd/SSE_SAMPLE.txt`
contains a live `/v1/events` heartbeat.

## Validation pass 2

`evidence/labd/VALIDATION_PASS_2.txt` records a clean package re-check
(`MANIFEST` all OK, spec validator PASS), regenerated fixture/cross-language
reports (44/44 and PASS), all Go packages, 36 Python tests, 3 Rust policy
tests, and provenance checks. `PROVENANCE_VALIDATION.json` shows 12,418/12,418
normalized live rows resolving to the raw offset and raw hash, with dataset,
quality, and archive/native hashes matching.

## Adversarial validation pass 3

`evidence/labd/VALIDATION_PASS_3.txt` records a second independent pass:
120 randomized canonical JSON vectors match byte-for-byte across Python, Go,
and Rust; both non-NFC inputs are rejected. The native clone, upstream tag,
Cargo.lock, spike source, binary, spike report, and fixture corpus all match
`locks/` (`evidence/native/PIN_VERIFICATION.json`). The suite is 37 Python
tests, all Go packages, and 7 Rust policy tests.

Negative checks found and fixed real gaps rather than only re-running the
existing harness:

- ledger `VerifyChain` accepted a deleted first event; sequence continuity is
  now checked and a tamper test covers first-event deletion.
- artifact reads already rejected a corrupted blob; a direct test now seals
  and then corrupts a blob to prove rejection.
- the evaluator verifier could raise on a missing or malformed signature; it
  now fails closed and tests cover missing, non-hex, and wrong-key cases.
- the dashboard catch-all returned HTML with status 200 for unknown paths; it
  now returns 404, rejects non-GET/HEAD, and sets CSP and nosniff headers.
- `qel-submit -wait` never treated `OUTPUT_SEALED` as complete and hung; the
  fixed binary sealed `JOB-VAL2-1794181441` and exited 0 in 12 s.
- normalization now verifies the raw segment hash and size before writing and
  a test confirms tampered input is rejected.

The real HTTP negatives are recorded in `evidence/labd/HTTP_NEGATIVE.txt`:
missing and wrong bearer tokens return 401, malformed JSON returns 400,
unknown paths 404, non-GET root 405, HEAD 200, dashboard CSP/nosniff present,
listener is `127.0.0.1:8787`, and token/socket/DB modes are 600/660/640.
Fixture mutation controls now catch 10/10 cases. Provenance checks additionally
prove every raw offset is used exactly once and row counts, partition size,
quality reference, time range, and reported gaps agree with the capture.

## Validation pass 4

`evidence/labd/VALIDATION_PASS_4.txt` is the latest consolidated pass: the
vendored spec tree is byte-identical to the extracted package, 120 canonical
vectors and 40 run-key vectors match across Python/Go/Rust, valid/invalid
schema decisions agree, 41 Python tests pass, all Go packages pass, and 7 Rust
policy tests pass.

Deeper checks found and fixed additional integrity gaps:

- `VerifyChain` now recomputes the stored payload hash and event ID, so editing
  `payload_json` or `event_id` without rehashing is detected.
- Re-sealing an artifact now verifies an existing blob and atomically repairs a
  corrupted blob instead of silently reusing it.
- normalization now records the capture's four loss/gap ranges in the quality
  certificate instead of publishing an empty gap list.
- the sealed evaluator uses `INSERT OR IGNORE` and refuses a concurrent second
  decision instead of overwriting the first.
- the bootstrap requires the full requested number of non-degenerate
  replicates; the four-generator 1,000-program B6 was rerun and remains 0
  promotions per generator with 95% upper 0.00299.
- control reports now state they match calendar and trade count only, and use
  documented NumPy PCG64 draws rather than claiming the execution HMAC seed
  protocol. `ASSUMPTIONS.md` records this as a development-only limitation.
- oracle metrics are unchanged when every `expected` field is removed, proving
  the fixtures are not self-fulfilling.
- native replays are bit-identical on rerun; unchunked and 2,000-event chunked
  runs match at 15 orders/8 positions, and the archive variant reconciles with
  residual 0.

The full B6 recheck took 508 s and is stored in
`evidence/labd/CALIBRATION_B6.json`; the bounded 100-program recheck is also
kept. The live ledger still verifies after rebuilding/restarting `qeld` and
`qel-worker`, and `JOB-VAL3-1794182958` sealed end to end.

## Measured resources

- Host: 6 vCPU, 11.7 GiB RAM, 132 GiB free (8% disk used).
- Native spike: 23 ms, 15.6 MiB peak RSS, 12.1 MB release binary.
- Conformance job: cgroup peak 77.4 MiB, pids peak 12, CPU 9.7 s.
- Capture job: cgroup peak 31.5 MiB, pids peak 7.
- Native replay scale: 12,418 events 408 ms / 20 MiB RSS; 124,180 events
  3.15 s / 53 MiB; 1,241,800 events 29.8 s / 367 MiB. The 100x corpus is a
  time-shifted repetition for scale/state growth only. Bounded replay in
  2,000-event chunks with `clear_data` matched the unchunked 15-order /
  8-position result at 19.9 MiB RSS.
- Archive day: 358,200 events (284,672 observed trades, 72,084 synthetic L1
  quotes, 1,440 marks, 4 funding settlements), 307 orders, 154 positions,
  8.5 s, 113 MiB RSS; independent accounting residual 0.
- Python tests: 41 passed. Go packages: ledger, artifacts, queue, control,
  memory, cgroup, submit pass. Rust policy: 7 tests pass.

## Running validations

`SOAK-24H` is RUNNING (started 2026-11-08T22:21:50Z). It has not elapsed and
must not be reported as complete. Verification is scheduled as
`sched_42815c00` for 2026-11-09T22:26:00Z. B6 completed four generators × 1,000 null
programs with zero promotions each (95% upper 0.00299); the injected-effect
power/MDE grid was executed locally and found zero power at the tested effects; a full MDE/coverage certificate remains open.

## Blockers

1. `CAP-LOCAL-VIEW`, `CAP-L2-QUEUE`, `CAP-ISOLATED`: pinned Nautilus exposes
   one shared cache and no queue-ahead/delayed-local-view hook. M4 is blocked.
2. OS principal separation: no passwordless sudo to create `qel-research`,
   `qel-eval`, `qel-worker`; the private evaluator uses a separate DB and key
   mode 0600 but not a separate Unix owner.
3. DuckDB 2.0.0 aborts with illegal instruction (CPU lacks AVX2); PyArrow is
   used for Parquet.
4. Fresh OOS: no untouched window and no authorization; TASK-28 remains
   `WAITING_FRESH_OOS`.
5. Forward data: 15 orders/15 fills/1 day vs required 200/50/10; TASK-29 is
   `INSUFFICIENT_OBSERVATION`.
6. Export: no capital authorization; TASK-30 refuses with
   `CAPITAL_UNAUTHORIZED`.

## Reproducible operation

```sh
cd /home/quant/quant/quant-edge-lab
./scripts/qelctl.sh start
./scripts/qelctl.sh status
./scripts/qelctl.sh restart
./scripts/qelctl.sh stop
```

Resume after reboot: `./scripts/qelctl.sh start`; the units are enabled and
linger is on. Rebuild: `make contracts`, `go build ./...`,
`(cd policy/rust && cargo test --release)`,
`(cd native/spike && CARGO_TARGET_DIR=/home/quant/quant/.build-cache/qel-native cargo build --release)`,
`(cd native/runner && CARGO_TARGET_DIR=/home/quant/quant/.build-cache/qel-native cargo build --release)`.

## Repository

Public repository: <https://github.com/ddawnlll/quant-edge-lab>
Branch: `main`; remote push succeeded. Final commit is the tip of `main`
after this report update.
Ponytail revision: `96608e078b2d5ea607c5c3b8b5a54d1c1fba6846` (MIT).
Upstream Nautilus revision: `27a8e54e7ac3c57d6cbf8891f0283dfbaee97317` (v1.231.0).

## Remaining issues

- M1/M2/M3 remain PARTIAL because only one archive day and a 240s live
  capture exist; there is no multi-day walk-forward family. The archive-day
  L1 quote is an explicit synthetic assumption around observed trades, not
  observed depth.
- B6 1000-program calibration, B0-B4 benchmark corpus, B5 restore drill and
  B7 7-day soak remain open.
- M4 cannot be certified until a backend with LocalView and queue hooks is
  selected; no fork or invented API was used.
- No positive edge exists or is claimed. The 12-variant development screen is
  `INSUFFICIENT_EVIDENCE`; all observed net PnL values are negative.
