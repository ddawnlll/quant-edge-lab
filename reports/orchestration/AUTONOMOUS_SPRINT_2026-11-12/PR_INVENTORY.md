# PR inventory — autonomous sprint 2026-11-12

Snapshot taken at sprint start from GitHub (`gh pr list`) plus local refs.
`origin/main` = `8b59f79` throughout unless noted. No branch protection and no
CI workflows exist in this repository, so "required checks" are the repository's
own documented commands.

## Dependency graph (verified, not assumed)

```
main 8b59f79
├── #38  qel/m2-full-coverage-a3-prep       (8a761b7, 2 commits, +10856/−113)
│     └── #41  qel/a3-streamed              (3678c6a, 5 commits ahead of #38)
├── #39  qel/b3-rootcause                   (3302ada, evidence only, 1 file)
├── #40  qel/task10-inventory               (3ace5f1, evidence only, 1 file)
├── #53  research/ta21-causal-foundation    (DRAFT, ad2b043)
└── #47  perf/v12-columnar-foundation       (e53fd7d, DRAFT, 4 files, +493)
      └── #48  perf/v12-p0-baseline         (1e3abbf, evidence only)
            ├── #49  perf/v12-p1-archive-parquet (8934680)
            │     └── #51 perf/v12-p3-vectorized (da9edbc, 10 commits)
            └── #50  perf/v12-p2-arrow-adapter  (a04493a)
```

Verified facts:

- `git merge-base --is-ancestor origin/qel/m2-full-coverage-a3-prep
  origin/qel/a3-streamed` → true. **#41 really is stacked on #38**; retarget to
  `main` only after #38 merges.
- `git merge-base --is-ancestor origin/perf/v12-p2-arrow-adapter-20261111
  origin/perf/v12-p3-vectorized-20261111` → true. **#50's content is already in
  #51.** #50 must not be merged separately if #51 lands.
- `git rev-parse origin/perf/v12-integration-rust-20261111^{tree}` equals
  `origin/perf/v12-p3-vectorized-20261111^{tree}` (both `57664bd4`). The branch
  used for PR #75 was merged into the branch underlying #51, not into `main`,
  and #51's tip is now identical to that integration tip. That is the documented
  historical detail confirmed against real objects.
- #49's base is `perf/v12-p0-baseline`; #51's base is `perf/v12-p1-archive-parquet`.
  So the chain #47→#48→#49→#51 is genuinely linear and must be merged in that
  order (or squashed together).

## PR status at sprint start

| PR | Branch | Base | State | Kind | Blocking facts |
|---|---|---|---|---|---|
| #38 | qel/m2-full-coverage-a3-prep | main | OPEN | evidence + 3 scripts + 2 tests | full suite 375 passed / 12 skipped at head; reviewer found 6 defects |
| #39 | qel/b3-rootcause | main | OPEN | evidence only | 1 JSON file; parse fast path verified reverted on main |
| #40 | qel/task10-inventory | main | OPEN | evidence only | 1 JSON file; inventory verified against disk and HTTP probes |
| #41 | qel/a3-streamed | #38 | OPEN | driver + control fit | 13 focused tests passed at head; reviewer found 7 defects |
| #47 | perf/v12-columnar-foundation | main | DRAFT | reader + tests | bounded PIT Parquet batch reader, fail-closed |
| #48 | perf/v12-p0-baseline | #47 | OPEN | evidence only | baseline anchor; no code |
| #49 | perf/v12-p1-archive-parquet | #48 | OPEN | Python ZIP→Parquet reference | keeps a legacy reference path |
| #50 | perf/v12-p2-arrow-adapter | #48 | OPEN | Rust core/adapter skeleton | **superseded by #51** (ancestor proof above) |
| #51 | perf/v12-p3-vectorized | #49 | OPEN | Rust ingest + adapter + runners | claims single-day differential PASS |
| #53 | research/ta21-causal-foundation | main | DRAFT | research draft | deferred: not on the critical path |

## Merge policy applied

The repository has **no branch protection and no CI**. The owner's instruction is
that a merge requires: relevant tests passing, no hidden acceptance blocker,
correct dependency base merged, an independent technical review completed, and no
overwriting of others' work. Because GitHub's review mechanism is not used by
this repository, the independent review is recorded as a review artifact on the
sprint branch and referenced from each PR comment. No PR is approved by its own
author's identity: the reviewer is a separate agent session, and the
coordinator records the verdict.
