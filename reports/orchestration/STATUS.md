# STATUS — 2026-11-10 night cycle, base 8b59f79
HEAD: 8b59f79 (main) + unmerged qel/m2-full-coverage-a3-prep 0c8af60
A2 FULL DONE: 546/546 fold days present and verified (testzip+sha256,
10,638,725,637 bytes, 18 monthly fundingRate zips, sealed untouched).
A3 PREP DONE: streaming tape builder byte-identical to canonical
archive.build (sha256 92bee987 for 2024-09-01), observed funding via
monthly fundingRate zips, 4 regression tests.
A3 RUN BLOCKED: tapes project ~90GB > 68GB free disk; ControlPlan needs
~39M quotes in RAM at fold scale; 6,552 variant-day replays unmeasured
(~51 CPU-h estimate). Streaming design recorded in
evidence/labd/M2_FOLD_TAPES_PILOT_2026-11-10.json. Dev-week M2 run
(M2_DEV_RUN.json) remains the valid development result.
B3 ROOT CAUSE (TASK-25): parse fast path evaluated, exactly equivalent
on 4.5M events, zero speedup — nautilus dispatch binds. Change
reverted, binary rebuilt clean (87337833). B3 stays FAIL_TARGET.
Next READY: B2 #3 venue PIT inventory, B3 #11 T-QUEUE (scoped),
C1 B3 engine profiling (deep, revalidation-gated).
Soak still ACTIVE, untouched.

## A3 streamed update (2026-11-11)
- Pilot parity proven: tape byte-identity, 12/12 family agreement, streamed plan hash == canonical (80b804c7…); pilot PASS / INSUFFICIENT_EVIDENCE.
- Measured forecast: 456 days ≈ 18.3h wall (14–23h range), ≤1.5GB RSS, ≤1.6GB disk. files: reports/orchestration/A3_FORECAST_MEASURED_2026-11-11.json, evidence/labd/A3_STREAMED_PILOT_2026-11-11.json.
- Stacked PR #41 (qel/a3-streamed → qel/m2-full-coverage-a3-prep). Do not merge without independent review.
- Full 365/91 run NOT launched; needs operator authorization.
- B3 profile addendum: parse 30% / engine.run 62% for 60s-clock variants; B3 remains FAIL_TARGET.
