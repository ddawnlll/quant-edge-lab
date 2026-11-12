# Night run — 2026-11-13 (UTC) — PROGRESS

Autonomous night run on `ddawnlll/quant-edge-lab`. All results are development only.

## Phase 0 — Snapshot

- `date -u` at start: 2026-11-12T21:55Z
- `origin/main` SHA: `eabe739` — "Merge pull request #95 from ddawnlll/sprint/orchestration-2026-11-12"
- Working tree at start: clean (local `main` was one merge behind; fast-forwarded to `origin/main`).
- Open PRs at start: **#41 only** (draft).
- Open issues at start: **31**.
- Test command (Makefile): `make test` → `.venv/bin/python -m pytest -q tests`
- Baseline suite on `origin/main` (`eabe739`): **423 passed, 14 skipped, 0 failed** in 248.76 s.

```
$ timeout 1800 .venv/bin/python -m pytest -q tests
423 passed, 14 skipped in 248.76s (0:04:08)
```

### Pre-existing state (important)

A manual hygiene batch ran immediately before this night run (outside this
journal) and already merged #93/#95, closed #50/#53, and closed the TA issues
#52 and #54–#74. This night run therefore found the TA backlog already closed
and **creates the tracking issues now**, after the fact, with a link to every
closed issue. No issue was re-opened (out of scope) and nothing was re-closed.

| HH:MMZ | phase | action | command | result |
|---|---|---|---|---|
| 21:55 | 0 | fetch + status + baseline | `git fetch origin --prune; git status; pytest -q tests` | PASS |

## Phase 1 — PR and issue hygiene

| HH:MMZ | phase | action | command | result |
|---|---|---|---|---|
| 22:00 | 1a | verify #93 (already merged at `57096bb`) | `git diff --stat 57096bb^1 57096bb` | PASS (11 files, +145/-29, 0 unexpected deletions) |
| 22:00 | 1a | verify #95 (already merged at `eabe739`) | `git diff --name-only eabe739^1 eabe739` | PASS (only `reports/`) |
| 22:02 | 1a | #75 close | `gh pr close 75` | BLOCKED (already MERGED into `perf/v12-p3-vectorized-20261111`; cannot close). Comment posted. |
| 22:02 | 1a | #75 diff check vs main | `git diff --numstat origin/main pr75` | +157/-12,335, 36 files, 15 deleted (prompt expected ~11,453; actual 12,335) |
| 22:02 | 1a | #50 close | already CLOSED 14:27Z by pre-run batch | PASS (comment "Superseded by #51") |
| 22:03 | 1a | #41 to draft | `gh pr ready 41 --undo` not needed | PASS (already draft; comment posted) |
| 22:03 | 1a | #53 close | already CLOSED 21:52Z by pre-run batch | PASS (branch kept; comment present) |

### Tracking issues created (1b)

- **#96** — TA hypothesis backlog (checklist of #52, #54–#74)
- **#97** — Perf decision: keep Nautilus, move screening to a vectorized layer
- **#98** — Live-path gates (frozen until a candidate exists)

Note: TA issues #52/#54–#74 were closed by the pre-run batch before #96 existed.
#96 lists them after the fact; no issue was re-opened or re-closed.

### Issue closures (1c)

- #80, #81, #82, #83, #84, #85, #87, #88, #89, #91 — `not planned`, linked #97.
- #86 — `not planned` (intentional fsync durability choice, not a bug).
- #92 — `completed` (root cause: ~19 µs/event Nautilus dispatch, PR #39; decision in #97).
- #7, #8, #9, #14, #16, #17, #18 — labeled `frozen` (label created), linked #98.
- #79 was already closed before this run (verified title, left as-is).

Issue counts: **31 open at start → 22 open at end** (12 closed, 3 tracking issues created).
Unexpected issues left untouched: none; every closed title matched its expected category.

Open issues remaining: 3, 7, 8, 9, 11, 13, 14, 15, 16, 17, 18, 42, 43, 44, 45, 46, 77, 78, 94, 96, 97, 98.

## Phase 2 — CI (issue #78)

| HH:MMZ | phase | action | command | result |
|---|---|---|---|---|
| 22:07 | 2 | branch `ci/basic-checks-20261113` | `git checkout -b ... origin/main` | PASS |
| 22:10 | 2 | add `.github/workflows/ci.yml` (python/go/rust, caching) | — | PASS |
| 22:11 | 2 | YAML sanity | `yq '.jobs | keys' .github/workflows/ci.yml` | PASS |
| 22:12 | 2 | go vet | `go vet ./...` | PASS (rc=0) |
| 22:13 | 2 | go test | `go test ./...` | PASS (all packages ok) |
| 22:14 | 2 | rust contracts | `cargo test --locked --manifest-path contracts/rust/Cargo.toml` | PASS (0 tests, rc=0) |
| 22:14 | 2 | rust policy | `cargo test --locked --manifest-path policy/rust/Cargo.toml` | PASS (3 passed) |
| 22:19 | 2 | python (CI-like empty cache) | `QEL_BUILD_CACHE=/tmp/empty-cache QEL_DATA_ROOT=/tmp/empty-data python -m pytest -q tests` | PASS (423 passed, 14 skipped) |
| 22:20 | 2 | open PR #100 | `gh pr create ...` | PASS |

Excluded from CI and why: Nautilus-dependent crates (`native/*`, `data-engine/*`)
need the large native build; tests that need binaries/corpus/systemd already skip
themselves through `tests/qel_paths.py` and `pytest.skip`; long A3/B6/B4 runs are
never started.

## Phase 3 — Mark source (issue #94)

| HH:MMZ | phase | action | command | result |
|---|---|---|---|---|
| 22:12 | 3a | locate builder/label/downloaders | `grep -rn observed_mark_price_kline_close ...` | done: `python/qel/prepare/archive.py` builds Mark from `--mark`; Python ingest scripts downloaded `klines/`, Go collector used `markPriceKlines` with the same basename |
| 22:25 | 3b | add `fetch_mark_one()` + CHECKSUM verify; distinct name | — | PASS |
| 22:22 | 3b | fetch dev week mark | `python scripts/m2_ingest_full.py --mark-only 2024-09-01 2024-09-07` | PASS (7 days, all checksums OK) |
| 22:30 | 3c | fail-closed Mark loader (Python canonical + parquet + stream) | — | PASS |
| 22:33 | 3c | Rust `load_marks` fail-closed | `cargo test -p qel-data-engine-ingest` | PASS (4 passed) |
| 22:35 | 3d | new tests | `pytest -q tests/test_mark_source.py tests/test_m2_exec_ingest.py` | PASS (6 + 13) |
| 22:37 | 3e | differential, full dev week | `python scripts/mark_source_diff.py --days 2024-09-01 ... 2024-09-07` | PASS (34m09s; `evidence/labd/MARK_SOURCE_DIFF_2026-11-13.json`) |
| 23:12 | 3d | full suite after fix | `.venv/bin/python -m pytest -q tests` | PASS (432 passed, 14 skipped) |
| 23:12 | 3f | stale-evidence doc | `docs/EVIDENCE_INVALIDATION_94.md` | PASS (nothing deleted) |
| 23:20 | 3g | open PR #101 | `gh pr create ...` | PASS |

Differential summary (old trade-close Mark vs real `markPriceKlines`):
max |Δ mark| 47.76378014–112.48683236 USDT; mark minutes changed 1150–1280/1440
per day; realized equity for `STRAT-VAR-00` changed by 0 on 4/7 days and by at
most 0.0074 USDT on the others; funding notional changed only on days where
funding fired.

## Phase 4 — screening skeleton (optional)

| HH:MMZ | phase | action | command | result |
|---|---|---|---|---|
| 23:17 | 4 | branch `research/screen-skeleton-20261113` | `git checkout -b ... origin/main` | PASS |
| 23:22 | 4 | funding-carry skeleton (numpy; pandas/polars absent) | — | PASS |
| 23:24 | 4 | tests | `pytest -q tests/test_screen_fast.py` | PASS (3 passed) |
| 23:25 | 4 | full suite | `pytest -q tests` | PASS (426 passed, 14 skipped) |
| 23:27 | 4 | draft PR #102 | `gh pr create --draft ...` | PASS |

Development-week screening result: naive funding carry is **-3.85633752 USDT net**
after explicit costs on 1000 USDT notional over 7 days. Screening only, no claim.

## Notable deviations

- #75 could not be closed: it was **already merged** into
  `perf/v12-p3-vectorized-20261111` before the run (`gh pr close` cannot close a
  merged PR). Verified diff vs `origin/main`: +157/-12,335 across 36 files (15
  deleted). Comment posted; no close.
- #41 was already a draft; `gh pr ready --undo` was unnecessary. Comment posted.
- #93 and #95 were already merged before the run (verified, not re-merged).
- #50 and #53 were already closed by the pre-run batch with the expected comments.
- The TA issues were already closed before #96 existed; #96 lists them after the
  fact. No issue was re-opened or re-closed.
