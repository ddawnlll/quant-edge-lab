# Post-merge validation, QEL v1.1

Post-fix note: the findings below were addressed in this follow-up. See
`POST_FIX_VERIFICATION.json` and `docs/V11_POST_MERGE_VALIDATION.md`.

Merged main: `166f8c1c06508e29b77477092b2c6a7c6586dfd4`
PR: https://github.com/ddawnlll/quant-edge-lab/pull/36 (MERGED)
PR head: `d7737967dec51fa9735af7eda9988dec6cd9ceb4`
Remote branches: `main` only.

## Result

`PASS_WITH_RECORDED_HYGIENE_FINDINGS`

| Check | Result |
| --- | --- |
| Post-merge state | PASS: PR MERGED, #4/#10 CLOSED, 11 target issues OPEN, only `main` remote |
| Evidence hash binding | PASS: all 7 integration hashes match; F22 source and binary hashes reproduce exactly at the recorded path |
| F22 invariants | PASS: exchange-before-delivery, zero policy lookahead with sequence ordering, exact response delay, feed delay once, exact convergence; zero-delay tape equals native (307 fills); rebuilt-binary rerun is byte-equal to the committed run on tape/f22/counts/local_view/exchange |
| Pre/post native equality | PASS: 14 common native probes identical before and after adapter completion, excluding elapsed_ms |
| Static analysis / contracts | PASS: go vet, gofmt, contract build, crosslang and runkey |
| Evidence path integrity | PASS: 36 referenced paths exist (one glob and one shell token excluded) |
| Adapter native coverage | PASS: 7 probes have profile boundary, native limitation and native behavior records; F02 unknown-aggressor remains intentionally PARTIAL |
| Passive adapter oracle | PASS: F04/F05/F06_CLIENT/F07/F29/F31 paths re-derived, 0 mismatches; rerun probes equal on all 21 semantics, only elapsed_ms differs |
| Open-issue blockers | PASS: #16 reserve 237 < 250/500; #17 15/15/1 insufficient; #18 capital refusal; #15 soak 0 gaps/0 bad health, B8 absent; #14 1,186.61 core-hours recomputed; #3 native liquidation limits confirmed |
| B3 under RP-8G | **FAIL_TARGET**: p90 203.617 s vs 120 s, 3 runs, 11,247,697 events; B3 now fails both profiles |
| B2 under RP-8G | PASS: 12 timed runs, p90 26.967 s, median 24.659 s, target 30 s; #13 still open on B3 10M |
| Benchmark corpus identity | PASS: B2 corpus 164,130,300 B / `731b213d...`; B3 corpus 1,484,328,129 B / `38a94cd1...`; all four B3 reports replay 11,247,697 events |
| B2/B3 statistics | PASS: B2 p90 26.452 s / median 24.292 s from 12 reports; B3 p90 209.741 s from 3 x 11,247,697-event reports, FAIL_TARGET retained |
| M2 report | PASS: report hash reproduced, 12+12 variants, 1,000 controls, 174,000 trades, STRAT-VAR-10, INSUFFICIENT_EVIDENCE with STATIONARITY_DIAGNOSTIC_INSUFFICIENT |
| Fresh clone | PASS with note: 643 tracked files, no data/runtime/build cache in clone; 70 core tests PASS; Go build PASS; LocalView and passive-runner release builds PASS; one non-hermetic M2 test fails without `data/` |
| Secrets/runtime data | PASS: no secret-like names or token/key patterns; no runtime/data/sqlite/key files tracked |
| Commit/branch audit | PASS: merge parents ce91981 + d773796; all 15 deleted remote tips are ancestors of main; no open PRs; git fsck clean |
| `pytest -W error` | PASS with note: 367 passed, 4 skipped, one cross-test sqlite finalizer warning; failing test passes in isolation |
| Evaluator/accounting entry points | PASS: evaluator-demo signed and fail-closed; archive accounting residual 0 exact |
| Isolation fail-closed | PASS: non-root provision exits 77; real-principal gate fails without principals; netns egress blocked |
| 20 USDT infeasibility | PASS: requested 20 vs 58.94189 venue minimum |
| Native pin/route | PASS: pin 12/12; workers high effort; root effort was low and config corrected |
| M2 reserve isolation | PASS: actual data dates are only the 7-day development week; 2026 dates are manifest metadata, not reads |
| Mini M2 rerun | PASS: 2 variants / 10 controls completed in 426.9 s from merged main; report hash reproduced, venue PASS, decision INSUFFICIENT_EVIDENCE |
| 240s live-capture F22 | PASS with finding: 15 fills at 50ms/25ms and 16 at 50ms/30s, all delayed, zero lookahead, feed delay once; 30s profile shows 1e-8 cash/equity drift versus the 8dp exchange account |

## Findings

1. **Info, F22-PATH.** The F22 evidence binds `/home/quant/quant/qel-lv/...`. Recreating that worktree reproduces the exact source hash (`b21c7f19...`), binary hash (`af79abd8...`) and byte-equal run output. The path is required for binary reproducibility because the source binding embeds `CARGO_MANIFEST_DIR`; future assemblers should emit a repo-relative path plus a separate reproduction path.
2. **Low, M2-TEST-HERMETIC.** `tests/test_m2.py::test_frozen_manifest_marks_missing_execution_days` assumes `data/native` exists. Proposed patch: `/home/quant/quant/.jcode-scratch-validate/fix_m2_test_hermetic.patch`, validated in the clean clone (`3 passed, 2 skipped`).
3. **Medium, TRACKED-BUILD-CACHE.** 151 files / 45 MB under `policy/rust/target` are tracked from commit `517a6c3`, before the audited reference. PR #36 added none of them. Separate hygiene cleanup: `git rm -r --cached policy/rust/target`.
4. **Medium, STALE-INTEGRATION-EVIDENCE.** `INTEGRATION_ISSUE04/10/13_VERIFY.json` still record the pre-adapter/pre-recheck state. Newer evidence supersedes them, but the old files stay in TASK/STATE evidence lists. Proposed patch: `/home/quant/quant/.jcode-scratch-validate/fix_stale_integration_evidence.patch`, validated with 60 focused tests.
5. **Medium, LOCALVIEW-8DP-DRIFT.** On the 240s live capture with `taker_1s` and a 30s response delay, LocalView cash is `999.4832680399999999999999999` versus exchange `999.48326803`, a 1e-8 USDT difference. Position and independent residual are exact, and the certified archive-day profile remains exact. Proposed fix: quantize delivered cash/equity to currency precision at response application and finalize.
6. **Medium, F22-BINARY-PATH.** Rebuilding `native/local_view` from the merged repo path produces binary hash `30e4b9b9...`, not the evidence hash `af79abd8...`, and `test_evidence_status_and_binding_hashes` fails because it asserts the binary hash whenever the binary exists. The recorded-path rebuild reproduces `af79abd8...` and passes. Proposed patch: `/home/quant/quant/.jcode-scratch-validate/fix_f22_binary_path_test.patch`.
7. **Low, WERROR-UNRAISABLE-SQLITE.** Full `pytest -W error` fails on an ignored `sqlite3.Connection` finalizer warning attributed to `test_control_zero_delay_equals_native_runner`. The test passes in isolation; normal pytest is 368 passed, 4 skipped. Close connections explicitly or filter that unraisable warning.
8. **Medium, ROOT-EFFORT-LOW.** Workers ran `deepseek-v4.1-flash` at high effort, but the root session reported `PI_REASONING_LEVEL=low` and config had `openai_reasoning_effort=low`. Config was corrected to high during validation; the already-running root process retains low effort.
9. **Info, STATE-PRE-MERGE.** `STATE.json`, `TASKS.md` and the matrix record the pre-merge state and tested code SHA `8b46425`. GitHub is authoritative post-merge.

No positive edge, fresh OOS result or live authority is claimed. The remaining issue gates are unchanged: #3 native liquidation/rules, #7/#9/#14 full B6 compute, #8 admin principals, #11 HftBacktest T-QUEUE, #13 B3 throughput/B4 RP-8G, #15 7-day soak/B8, #16 fresh OOS, #17 forward observations, #18 capital authorization.
