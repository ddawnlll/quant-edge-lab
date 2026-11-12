# Sprint progress journal — 2026-11-12

Append-only. Status labels: `NOT_STARTED`, `IN_PROGRESS`, `IMPLEMENTED_UNVERIFIED`,
`TESTED`, `BLOCKED_VALIDATION`, `BLOCKED_BACKEND`, `BLOCKED_AUTHORIZATION`,
`SUPERSEDED`, `MERGED_VERIFIED`, `FAILED`.

All timestamps UTC.

## Environment facts (verified at start)

- Repo root `/home/quant/quant/quant-edge-lab`; start HEAD `3945232` on
  `perf/v12-integration-rust-20261111`; `origin/main` = `8b59f79`.
- `main` is **not** branch-protected; no rulesets; **no CI workflows** exist
  (`.github/workflows` absent). Verification is the repository's own commands.
- Running services left untouched all sprint: `qeld` (pid 800710),
  `qel-worker` (pid 822946), `soak.sh` (pid 702661). `/healthz` returned 200
  after every branch operation.
- Host: 6 vCPU, 11.7 GiB RAM, 30 GB free at start; `data/` 32 GB gitignored.
- Jcode v0.99.1000-dev; `meta/muse-ember` reachable via provider `command-code`
  (smoke test returned `EMBER_OK`).

## Delegation limitation (recorded, not hidden)

Sub-agent sessions were spawned via `jcode run -m meta/muse-ember`. The 200k
context ceiling is a **per-session harness request** (`set_context_cap`,
`docs/HARNESS.md`); the one-shot `jcode run` CLI exposes no `--context-cap`
flag, so delegated runs used the model window bounded by explicit file scoping.
Reported upload for the reviewer session: 135,523 tokens. This is a tooling
limitation, not a scientific one, and it is why every delegated finding was
**re-verified by the coordinator** before any fix was accepted.

## Checkpoints

### 12:39Z — Worktrees and shared data
- Created `qel-sprint-a2` (#38 head `8a761b7`), `qel-sprint-a3` (#41 head
  `3678c6a`), `qel-sprint-data` (#51 head `da9edbc`), `qel-sprint-90` (main),
  `qel-sprint-orch` (main), `qel-sprint-47`, `qel-sprint-40`.
- `data/` symlinked into each worktree; it is gitignored, so nothing can be
  committed through the link and no data is duplicated (disk stayed ~27 GB free).

### 12:45Z — PR #38 baseline: TESTED
- Focused `test_m2_exec_ingest.py test_m2_fold_tapes.py`: 9 passed.
- Full suite at `8a761b7`: **375 passed, 12 skipped** (195 s).
- Independent coverage re-derivation (scratch script, no repo imports):
  546 needed days, 546 present, 0 missing; newest needed day 2026-02-28 <
  sealed 2026-03-08; 12 sampled day zips re-hashed + CRC: 0 mismatches;
  18 funding zips, 1,638 rows, 0 hash mismatches.

### 12:52Z — PR #38 independent review: FAILED (6 defects)
Reviewer session reported 6 findings. Three reproduced by the coordinator:
non-monotonic tape accepted; out-of-day funding silently dropped; `funding=0`
returned as `ok`. Status: `IN_PROGRESS` -> fixes required.

### 13:04Z — PR #38 fixes: TESTED
Commit `5b565f9`. Focused 19 passed; broad **393 passed, 4 skipped**.
Independent reproduction of each fixed behaviour recorded in the PR comment.

### 14:47Z — PR #41 fixes: TESTED, not merged
Commits `9c0b067` (seven review defects) and `3b49ac8` (hermetic driver tests),
rebased twice (13:57Z, then 14:38Z) because the branch carried pre-fix A2 copies
and would have reverted them.
Final head `3b49ac8`: focused **27 passed** (84 s); full suite
**447 passed, 5 skipped** (353 s).
Status stays **OPEN / review-ready**: the coordinator authored these fixes, so
the author does not clear the final gate. A detailed summary is posted on the PR.
An earlier draft of FINAL_REPORT.md wrongly listed #41 as merged; corrected.

### 13:45Z — PR #38 MERGED_VERIFIED
Merge commit `eb86a38`. Post-merge focused re-run on `main`: 24 passed.
Verified `git show eb86a38 --stat` contained the expected files.

### 13:46Z — PR #39 MERGED_VERIFIED (`61f15ca`)
Evidence only. Cross-checked every quoted B3 number against
`B3_10M_ATOMIC_L2_2026-11-10.json` (209.741 s, 120 s, FAIL_TARGET, 11,247,697
events) and confirmed the parse fast path is absent from `main`'s runner.

### 13:47Z — PR #40 MERGED_VERIFIED (`6810284`) with corrections
Re-probe contradicted one row (mark-price sources ARE available). Corrected the
inventory in `0896314`, recorded the mislabelled Mark tape, and filed
**issue #94** with measured evidence (1440/1440 minute closes differ, max |Δ|
52.787 USDT). TASK-10 stays PARTIAL; issue #3 stays open.

### 13:54Z — PR #47 hardening: TESTED
Commit `14b66cf`. Three failures reproduced pre-fix
(`DID NOT RAISE ArrowPartitionError`): float column accepted, `recv_ns < ts_ns`
accepted, regressing `raw_offset` accepted. 14 -> 17 passed.

### 14:16Z — PR #47 MERGED_VERIFIED (`38e4aff`)
### 14:17Z — PR #48 MERGED_VERIFIED (`be3d736`)
Evidence anchor; independently re-derived its Parquet sha and replay PASS.
### 14:18Z — PR #49 MERGED_VERIFIED (`a64faed`)
### 14:27Z — PR #51 MERGED_VERIFIED (`6e81f88`)
Rebased to `0b48a38` first (pre-rebase diff would have deleted merged tests).
At that head: Rust **18 passed**; Python **429 passed, 5 skipped**; sealed
refusal exit 1. Coordinator independently reproduced: ingest Parquet sha
`8fea0122…` identical, decode byte-parity identical, replay differential PASS,
and the funding-positive differential (issue #76) PASS.
### 14:27Z — PR #50 CLOSED / SUPERSEDED (ancestor proof recorded)
### 14:28Z — Issues #76 and #90 CLOSED with measured evidence.

### 14:32Z — Final integrated `main` (`6e81f88`) verification
Broad suite re-run on `main`; `go vet ./...` clean; merged-fix presence
re-asserted by grep on `origin/main` blobs
(`NON_MONOTONIC_AGGTRADES`, `funding_for_day`, `PIT_FLOAT_COLUMN`,
`CHUNK_BYTES_UNSUPPORTED_FOR_PARQUET` all present).

## Resource observations

- Never more than one compile at a time; all builds `-j2`, `nice -n 10`.
- No `cargo clean`; reused `/home/quant/quant/.build-cache` and the existing
  `native/runner/target` (4.1 GB) throughout.
- Free disk moved 30 GB -> 27 GB (scratch parquet/tape artifacts, all under
  `~/.jcode-scratch-sprint`, none committed).
- One 5-minute stall caused by rebuilding a real 1.25M-event tape inside driver
  tests; fixed by making those tests hermetic (11.7 s instead of minutes).

## Remaining blockers (unchanged by this sprint)

`BLOCKED_AUTHORIZATION`: #8 principals, #16 fresh OOS, #18 capital.
`BLOCKED_VALIDATION`: #11 queue differential, #13 B3 target, #14 B6 budget,
#15 7-day soak (ends 2026-11-16T22:26Z), #17 forward observations.
`BLOCKED_BACKEND`: native liquidation path for TASK-10.
`NOT_VERIFIED`: macOS/arm64 portability (#90), 14-day P4 A/B, worker sweep.
