# Decisions — 2026-11-12

Each decision names the evidence that drove it and what was deliberately *not*
done.

## D1 — Fix PRs #38/#41 before merging, instead of merging and fixing later

**Evidence.** An independent reviewer session (separate agent, not the
implementer) reproduced six defects in #38 and seven in #41. I independently
reproduced the three semantic ones before fixing:

- a non-monotonic aggTrades CSV produced a descending-timestamp tape that
  differed from the canonical builder while the PR claimed byte identity;
- `build_day` silently dropped funding rows outside the UTC day, diverging from
  canonical byte output with no error;
- `build_day` returned `status: ok, funding: 0`, i.e. a silent funding gap, on
  the exact class of day the pipeline exists to measure.

**Decision.** Fix on the PR branches with regression tests. Do not merge a
pipeline whose central claim (byte identity with the reference) is conditional
and unenforced.

**Consequence recorded honestly.** Byte identity is now *conditional on an
enforced input contract*. Out-of-contract input fails closed. That is strictly
better than a silent divergence, but it means the builder refuses inputs the
canonical builder would reorder rather than reproducing them.

## D2 — Rebase A3 onto merged main instead of copying files across

**Evidence.** After #38 merged, the A3 branch still carried the pre-fix copies of
`scripts/m2_build_fold_tapes_stream.py` and `tests/test_m2_fold_tapes.py`. A
plain merge of #41 would have **reverted** the A2 fixes. The same trap existed
for #51 against the merged P-chain: `git diff --stat` showed deletions of
`tests/test_arrow_batches.py` and `tests/test_m2_fold_tapes.py`.

**Decision.** Rebase each stacked branch onto the new `main` before merging, then
re-run the full suite. This is the reason the P-chain was merged in dependency
order (#47 → #48 → #49 → #51) rather than in parallel.

**Invariant.** A merge must never delete another PR's fix. Verified with
`git diff --diff-filter=D --name-only origin/main...HEAD` returning empty.

## D3 — Do not merge #50 separately

**Evidence.** `git merge-base --is-ancestor perf/v12-p2-arrow-adapter
perf/v12-p3-vectorized` is true, and `git rev-parse` of both integration trees
returns the same object `57664bd4`. #50's content is already in #51.

**Decision.** Close #50 as superseded once #51 lands, citing the replacing
commit. No double-merge.

## D4 — Correct PR #40 rather than merge it as written

**Evidence.** The inventory recorded `premiumIndexKlines` as
`NOT_FOUND_AT_PROBED_PATHS`. My re-probe returned **HTTP 200** for both
`premiumIndexKlines` and `markPriceKlines`, daily and monthly.

While checking that, I found a larger problem by measurement: the canonical tape
builder labels its `Mark` rows `observed_mark_price_kline_close`, but the input
is the 1m **trade** kline close. On 2024-09-01 **all 1440** minute closes differ
from the real mark series, max |Δ| **52.787 USDT**.

**Decision.** Correct the inventory in place, record the provenance defect, and
file **issue #94** with acceptance criteria. Do **not** change the mark source in
this sprint: that changes every existing replay result and needs its own
differential, re-run evidence, and updated fidelity blocks. TASK-10 stays
`PARTIAL`; issue #3 stays open.

## D5 — Reject `--chunk-bytes` on the Parquet path instead of redefining it

**Evidence.** The Parquet reader incremented a counter by 1 per event and
compared it to `--chunk-bytes`, so a byte budget silently became an event count.
Two runs could therefore flush at different boundaries while claiming to be the
same measurement.

**Decision.** Fail closed: `CHUNK_BYTES_UNSUPPORTED_FOR_PARQUET: use --chunk
<events>`. `--chunk` already produces identical boundaries to the legacy runner,
which is what the differential needs. Redefining a documented flag silently is
worse than refusing it.

## D6 — Prove the funding-positive differential from existing data

**Evidence.** Issue #76 claimed no funding-positive day was available. Scanning
the 180 archived pilot reports showed **73** variant-days with a non-zero funding
count.

**Decision.** Use 2024-09-02 / `STRAT-VAR-00`: funding count 1, total
**0.00021238 USDT** in both paths, equity `986.08372330` in both, tape 664
entries both, no divergence. No new data acquired, no sealed data touched.

## D7 — Leave the soak, services and data untouched

**Evidence.** `qeld` (pid 800710), `qel-worker` (pid 822946) and `soak.sh`
(pid 702661) were running at sprint start and still run at sprint end; health
returned 200 after every branch operation. All heavy work used `nice -n 10`,
`-j2`, and one compile at a time.

**Decision.** No restart, no `cargo clean`, no service reconfiguration, no data
deletion. The 7-day soak window (ends 2026-11-16T22:26Z) is left to complete.

## D8 — Do not implement the TA21 backlog or the performance micro-list

**Evidence.** Twelve filed micro-optimizations sum to roughly 3–4% of the
~20.1 µs/event cost; the rest is Nautilus dispatch (issue #92, PR #39). TA21
families are hypotheses with no validated math or causal acceptance yet.

**Decision.** Neither is a blocker, and implementing them would add surface area
without producing a defensible result. Left deferred for the recorded reasons.

## D9 — Record "untested" as untested

**Evidence.** No macOS or arm64 host was available.

**Decision.** Issue #90's portability fix removes machine-specific paths and is
verified on Linux only. The macOS acceptance criterion is recorded as
**untested**, never as passing. Same for the 14-day P4 benchmark, the worker
sweep, B6, and the 7-day soak: each is named as pending, not implied complete.
