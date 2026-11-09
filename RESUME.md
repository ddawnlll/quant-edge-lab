# Resume

Updated: 2026-11-09T11:38Z. Repository:
<https://github.com/ddawnlll/quant-edge-lab>. Integration branch
`qel/int-20261109`, PR #19. Base `f6dbde793e9db6c6124f4826b5bdff7e793502c1`.

## State in one paragraph

The verifiable issue work is integrated and independently checked on PR #19:
TASK-06 (PIT/provenance), TASK-08 (native feasibility), TASK-11 (scoped taker
certificate), TASK-13 (HMAC controls), TASK-14 (paired decision), TASK-15
(evaluator anchor/consumption, T-ISOLATION blocked) and TASK-16 (signed
synthetic cohort release). Python 164 tests, Go `./labd/...`, Rust policy tests,
the spec manifest and the offline validator pass. No positive edge is claimed
and no live trading authority exists. The 24-hour soak is still running; the
7-day stage and B8 remain open. M4 queue/LocalView capabilities are
`BLOCKED_BACKEND` in the pinned Nautilus commit.

## Verified on the integration branch

- **#1 / TASK-06**: per-row exact line hash + offset for all 12,418 rows, full
  5,145,372-byte coverage, bitemporal valid+known-time rules, sequence/snapshot/
  quote/duplicate/timestamp negatives, `UNIVERSE_UNVERIFIED` gate, DuckDB SIGILL
  reproduced and PyArrow/Parquet certified. The upstream-response `raw_sha256`
  is not recomputable from the frame and is explicitly scoped.
  `evidence/labd/INTEGRATION_ISSUE01_VERIFY.json`.
- **#2 / TASK-08**: pin/source/lock/license/toolchain/binary/corpus/resource
  manifest `locks/NATIVE_FEASIBILITY_MANIFEST.json`; pin verification 12/12;
  spike semantic recheck equal; F01/F08/F15 PASS in 44/44 conformance;
  LocalView/L2 queue/isolation `BLOCKED_BACKEND` with exact missing hooks.
- **#4 / TASK-11**: funding now on the primary tape with deterministic
  settlement identity and attributable epoch; archive day 358,200 events,
  307 orders, 154 positions; independent cash/equity/position exact with
  residual 0; remove/repeat/late/future-epoch/after-END mutations visible or
  rejected; semantic replay and chunk parity identical. F22 remains blocked.
  `evidence/labd/INTEGRATION_ISSUE04_VERIFY.json`,
  `evidence/labd/INTEGRATION_CHUNK_PARITY.json`.
- **#6 / TASK-13**: `HMAC_SHA256_COUNTER_V1`, 1,000 seeds, private account per
  seed, batch==sequential, train-only fit, no-lookahead tests, per-stratum
  diagnostics, deterministic L1 reservoir, all trials preserved. F27 PARTIAL
  and B1 OPEN because only one archive day exists.
  `evidence/labd/INTEGRATION_ISSUE06_VERIFY.json`.
- **#7 / TASK-14**: shared calendar/capital, shared bootstrap indices,
  per-replicate HAC, IUT + Holm, conservative MC bound, WAITING_COMPUTE_BUDGET,
  power/MDE and reason codes. The profitable-candidate/stronger-control audit
  returns NO_PROMOTION; the production `stats-job` never returns EDGE for it.
  `evidence/labd/INTEGRATION_ISSUE07_VERIFY.json`.
- **#8 / TASK-15**: outside-root anchor chain, pre-access receipt, irreversible
  consumption, one-winner race, restore/rename/copied-registry refusal, signed
  batch verification. **T-ISOLATION is BLOCKED** on administrator-created
  `qel-research`/`qel-eval` principals, socket ownership and worker network
  isolation. `evidence/labd/INTEGRATION_ISSUE08_VERIFY.json`,
  `evidence/labd/ISOLATION_LIMITATIONS.json`.
- **#9 / TASK-16**: caller-supplied metrics/unsigned code/bad lineage/unknown
  keys rejected; G0-G4 fail closed; multi-candidate synthetic cohort through
  freeze -> consume -> one signed batch; invalid candidate stays in the Holm
  family at p=1; failed gate blocks promotion; retry/tamper/duplicate/
  changed-input/contamination negatives; redacted manifest and independent
  signature verification. Market OOS/G4 remains external.
  `evidence/labd/INTEGRATION_ISSUE09_VERIFY.json`.

- **#3 / TASK-10**: point-in-time venue fee/funding/margin-tier/mark/status rules, funding epoch attribution, duplicate and late settlement handling, shared-wallet reservations, mark-triggered isolated forced close and exact venue-vs-account reconciliation. F08/F09/F10/F11/F12/F13/F16 pass 10/10 scenarios. Full acceptance remains EVIDENCE_PENDING on venue-certified historical rules and a native liquidation path. `evidence/labd/INTEGRATION_ISSUE03_VERIFY.json`.
- **#14 / TASK-26**: exact B6 workload implemented with the four frozen generators, HMAC labels, 16 variants, 3 cohorts, 1500/500 days, paired decision, coverage and power reports. The bounded pilot is `PILOT_NOT_ACCEPTANCE`; estimated full B6 is 1,085.64 core-hours, so the 1000-program FWER certificate is blocked on compute. `evidence/labd/INTEGRATION_ISSUE14_VERIFY.json`.

## Open gates (do not claim)

1. **#8 / TASK-15**: create `qel-research` and `qel-eval` principals, chown the
   sealed root/key/DB, restrict the authorized socket, and run the evaluator
   with a worker network namespace. Then rerun `tests/test_isolation_anchor.py`
   with real UIDs and attach the result.
2. **#6 / TASK-13, #5 / TASK-12**: only one archive day exists. Obtain eligible
   multi-day history, then rerun the 1,000-seed controls, walk-forward splits
   and B1/F27/T-PROTOCOL acceptance.
3. **#10/#11 / TASK-17/18**: no L2 queue or LocalView hook in the pinned
   backend. Adopt a supported backend only through an ADR and rerun TASK-08 and
   affected certificates.
4. **#12 / TASK-19**: byte-bounded replay and the B2-B4 1M/10M/100M plateau are
   not implemented; event-count chunk parity is verified but the runner still
   materializes the whole vector.
5. **#13 / TASK-25**: B0-B5 release benchmarks under 4 GiB and 8 GiB are not run.
6. **#14 / TASK-26**: full 1000-program B6 FWER/coverage/MDE certificate remains; the protocol is implemented but the estimated compute is 1,085.64 core-hours on this host. #7 and #9 stay B6-pending until that report exists.
7. **#15 / TASK-27**: 24h soak matures at 2026-11-09T22:21:50Z; a scheduled
   verification wakes at 22:26Z (`sched_1a3a7492`). The 7-day stage and B8 are
   open; a second wake is set for 2026-11-16T22:26Z (`sched_d9b2017c`).
8. **#16 / TASK-28**: `WAITING_FRESH_OOS`; no untouched window or confirmation
   authority.
9. **#17 / TASK-29**: `INSUFFICIENT_OBSERVATION`; 15 orders/1 day vs the
   required 200 orders/50 fills/10 days, and no capital authorization.
10. **#18 / TASK-30**: live activation refuses `CAPITAL_UNAUTHORIZED`; offline
    export software can be exercised but real execution is outside this run.

## Commands

```sh
cd /home/quant/quant/qel-int
PYTHONPATH=python QEL_ROOT=$PWD /home/quant/quant/quant-edge-lab/.venv/bin/python -m pytest -q tests
go test ./labd/... -count=1
(cd policy/rust && CARGO_TARGET_DIR=/home/quant/quant/.build-cache/qel-policy cargo test --release)
(cd specification && sha256sum -c MANIFEST.sha256)
/home/quant/quant/quant-edge-lab/.venv/bin/python specification/validation/validate_spec.py --report /tmp/qel_validation_report.json
```

## Environment notes

- Go 1.27.3, Rust 1.99.1, Python 3.13.5, 6 vCPU, 11.7 GiB RAM.
- DuckDB 2.0.0 aborts with SIGILL on this CPU; PyArrow 26.0.1 is the certified
  Parquet path.
- No passwordless sudo: OS-owner separation and privileged socket/network
  changes remain blocked.
