# QEL v1.1 post-merge validation and follow-up fixes

Base merged main: `166f8c1c06508e29b77477092b2c6a7c6586dfd4`.
This follow-up applies the validated fixes from the post-merge audit and
publishes the audit and extra measurements. It does not reopen or re-close any
issue and does not claim a new scientific result.

## Applied fixes

| Finding | Fix |
| --- | --- |
| `LOCALVIEW-8DP-DRIFT` | LocalView cash, fees and funding are quantized to 8 decimal places at response application and finalize. The archive-day and 240s profiles now converge exactly. |
| `F22-BINARY-PATH` | `tests/test_local_view.py` asserts the binary hash only when the recorded source path exists; otherwise it skips with the source hash as the stable binding. |
| `M2-TEST-HERMETIC` | `tests/test_m2.py` skips the data-dependent manifest check when `data/native` is absent. |
| `STALE-INTEGRATION-EVIDENCE` | Issue-04/10/13 verification records now carry `superseded_by` and `post_merge_status`. |
| `TRACKED-BUILD-CACHE` | 151 pre-existing files under `policy/rust/target` were removed from the index. Files remain on disk; `.gitignore` already excludes `target/`. |

## Post-fix verification

- F22 archive-day evidence regenerated with the fixed source and binary:
  all four invariant groups and the 30s/90s delivery-gap checks pass.
  Source `678260ee...`, binary `ad897f99...`.
- 240s live capture with `taker_1s`:
  main 15 fills, 30s gap 16 fills, zero-delay control 15 fills; cash,
  equity and position differences are exactly zero in all three.
- Focused suite: 65 passed (`test_local_view`, `test_m2`,
  `test_benchmarks`, `test_passive_queue_adapter`).
- B2 RP-8G: p90 26.967 s, median 24.659 s, target 30 s, PASS.
- B3 10M RP-8G: p90 203.617 s, target 120 s, `FAIL_TARGET`.
- Mini M2 rerun: 2 variants / 10 controls, report hash reproduced.
- Native pin: 12/12 checks PASS.
- Isolation negative checks: non-root provisioning exits 77, real-principal
  gate fails closed, worker netns egress blocked.
- 20 USDT target: `INFEASIBLE_VENUE_MIN_NOTIONAL` against the 58.94189
  minimum.

## Evidence

`evidence/labd/validation-2026-11-10/` contains:

- `POST_MERGE_VALIDATION.json` / `.md`: the pre-fix audit with 33 checks and
  9 findings.
- `POST_FIX_VERIFICATION.json`: applied fixes, new F22 bindings and 240s
  results.
- `B2_RP8G_RECHECK.json`, `B3_10M_RP8G.json`, raw report archives.
- `LIVE240_1s_main.json`, `LIVE240_1s_gap.json`,
  `LIVE240_1s_control.json`.
- `M2_DEV_RUN_MINI.json`, `M2_USDT20.json`.
- `PIN_VERIFICATION_FINAL.txt`, `CROSSLANG_FINAL.json`,
  `RUNKEY_FINAL.json`, `EVALUATOR_DEMO_FINAL.json`,
  `NATIVE_FUNDING_ACCOUNTING.json`.
- `patches/`: the validated patches applied in this follow-up.

## Remaining external gates

`#3` native liquidation/rules, `#7/#9/#14` full B6 compute, `#8`
administrator-created principals, `#11` HftBacktest T-QUEUE, `#13` B3 10M
throughput, `#15` 7-day soak/B8, `#16` fresh OOS authority, `#17` forward
observations, `#18` capital authorization.

No positive edge, fresh OOS result or live trading authority is claimed.
