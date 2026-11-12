# Final report — autonomous night run 2026-11-13

Repo: `ddawnlll/quant-edge-lab`
Window: 2026-11-12T21:55Z → 2026-11-12T23:30Z (started early; stopped new work
before 04:30Z). All results are **development only**; no profitability claims.

Baseline `origin/main`: `eabe739` (merge of #95).

---

## 1. PRs touched

| PR | Action | Reason |
|---|---|---|
| [#93](https://github.com/ddawnlll/quant-edge-lab/pull/93) | **already merged**, verified | Portability build fix. `git diff --stat 57096bb^1 57096bb` = 11 files, +145/−29, `--diff-filter=D` empty. No re-merge. |
| [#95](https://github.com/ddawnlll/quant-edge-lab/pull/95) | **already merged**, verified | Docs only: `git diff --name-only eabe739^1 eabe739` touches only `reports/` (6 files, +737). |
| [#75](https://github.com/ddawnlll/quant-edge-lab/pull/75) | **comment only — could not close** | Already MERGED into `perf/v12-p3-vectorized-20261111` before the run. `gh pr close` refuses merged PRs. Diff vs `origin/main`: +157/−12,335 across 36 files (15 deleted). Comment warns not to merge into main. |
| [#50](https://github.com/ddawnlll/quant-edge-lab/pull/50) | already CLOSED (pre-run batch) | "Superseded by #51"; verified state and comment. |
| [#41](https://github.com/ddawnlll/quant-edge-lab/pull/41) | **draft confirmed + comment** | Already draft, so `gh pr ready --undo` was unnecessary. Comment: parked until the research direction is decided; code stays review-ready. |
| [#53](https://github.com/ddawnlll/quant-edge-lab/pull/53) | already CLOSED (pre-run batch) | Branch kept; TA operators to be revisited in the lightweight screening layer. |
| [#99](https://github.com/ddawnlll/quant-edge-lab/pull/99) | **opened** | Night journal + this report. |
| [#100](https://github.com/ddawnlll/quant-edge-lab/pull/100) | **opened** | CI: Python/Go/Rust basic checks (#78). |
| [#101](https://github.com/ddawnlll/quant-edge-lab/pull/101) | **opened** | #94: real `markPriceKlines`; fail closed. |
| [#102](https://github.com/ddawnlll/quant-edge-lab/pull/102) | **opened (draft)** | Vectorized screening skeleton. |

No PR was merged by this run. #93/#95 were already merged; every new PR is open.

---

## 2. Issues

- Open at start: **31**.
- Closed by this run: #80–#85, #87–#89, #91 (`not planned`, linked #97),
  #86 (`not planned`, intentional fsync durability), #92 (`completed`, root cause
  in #97), and #92's decision recorded. (#79 was already closed.)
- Created: **#96** TA hypothesis backlog, **#97** perf decision, **#98** live-path gates.
- Open at end: **22** (3, 7, 8, 9, 11, 13, 14, 15, 16, 17, 18, 42–46, 77, 78, 94, 96, 97, 98).
- Labeled `frozen` (label created): #7, #8, #9, #14, #16, #17, #18.
- Left open deliberately: #3, #11, #13, #15, #77, #78, #94, the PERF v1.2 epic/sub-issues (#42–#46).

**Unexpected issues:** none. Every closed title matched its expected category.
Note: the TA issues #52/#54–#74 were closed by the pre-run manual batch before
#96 existed; #96 lists them after the fact. No issue was re-opened or re-closed.

---

## 3. Test counts

| Suite | Baseline (`eabe739`) | End of night |
|---|---|---|
| Python `pytest -q tests` | **423 passed, 14 skipped, 0 failed** | **432 passed, 14 skipped, 0 failed** on `fix/94` (9 new tests); **426 passed, 14 skipped** on `research/screen` (3 new tests) |
| Go `go vet ./...` | rc=0 | rc=0 |
| Go `go test ./...` | all packages ok | all packages ok |
| Rust `contracts/rust` | 0 tests, rc=0 | 0 tests, rc=0 |
| Rust `policy/rust` | 3 passed | 3 passed |
| Rust `data-engine/ingest` | (not in baseline gate) | 4 passed (includes the new mark-source rejection) |

No test was deleted. No suite is worse than baseline.

---

## 4. Issue #94 — Mark source

**Root cause.** The tape builders wrote `Mark` rows from the 1m *trade* kline
close and labelled them `observed_mark_price_kline_close`. Binance names the
trade kline and the mark kline identically (`<SYM>-1m-<date>.zip`); the Python
ingest scripts downloaded `klines/`, and the Go collector wrote
`markPriceKlines` under the same basename, so download order decided provenance.

**What changed.**
- `python/qel/prepare/archive.py`: `load_mark_rows()` validates source
  (`volume == 0` and `quote_volume == 0` for every row) and raises
  `MarkSourceError: MARK_SOURCE_MISSING` on missing/empty/wrong data and
  (opt-in) short days. `build()` gained `mark_source`/`require_full_day`.
- `python/qel/prepare/archive_parquet.py`: same guard and label.
- `scripts/m2_build_fold_tapes_stream.py`: reads the distinct
  `BTCUSDT-markPriceKlines-1m-<day>.zip`, fails closed via the shared loader.
- `data-engine/ingest/src/lib.rs`: `load_marks()` rejects non-zero volume.
- `scripts/m2_ingest_full.py`: `fetch_mark_one()` verifies the official
  `.CHECKSUM`, stores the distinct filename, refuses sealed days.
- `labd/collect/collect.go`: mark file stored under the distinct name.
- `scripts/run_parquet_replay.sh`: `--mark` points at the real mark file.
- Fallback is explicit only: `mark_source="trade_kline_close"` →
  `trade_kline_close_proxy_NOT_MARK`; never the default.

**Differential (dev week, old vs new, `STRAT-VAR-00`).**

| day | max abs Δ mark | equity old → new | funding count/total old → new | orders / positions |
|---|---|---|---|---|
| 2024-09-01 | 52.78739716 | 986.26990116 → 986.26990116 | 0 / 0 → same | 652 / 326 |
| 2024-09-02 | 112.48683236 | 986.08372330 → 986.08372330 | 1 / 0.00021238 → same | 660 / 330 |
| 2024-09-03 | 56.28819858 | 986.44717752 → 986.45453752 | 0 / 0 → same | 641 / 321 |
| 2024-09-04 | 90.74774848 | 984.67655088 → 984.67655088 | 0 / 0 → same | 664 / 332 |
| 2024-09-05 | 63.94247518 | 986.03690282 → 986.03690269 | 1 / −0.00115682 → −0.00115695 | 702 / 351 |
| 2024-09-06 | 74.26315624 | 986.92296940 → 986.91994940 | 0 / 0 → same | 705 / 353 |
| 2024-09-07 | 47.76378014 | 987.85899798 → 987.85899749 | 1 / −0.00414206 → −0.00414255 | 646 / 323 |

Evidence: `evidence/labd/MARK_SOURCE_DIFF_2026-11-13.json`. Mark rows per day
1440 in both tapes; mark minutes changed 1150–1280/1440. Equity moved by at most
0.0074 USDT on 4/7 days and by 0 on the other 3 — a property of this strategy,
not evidence the old tape was right.

**Stale evidence** (listed, not deleted): `docs/EVIDENCE_INVALIDATION_94.md`.
Highlights: `data/native/_fold/2024-09-01…07`, `DS-ARCHIVE-BTCUSDT-2026-11-01…07`,
`evidence/labd/M2_FOLD_TAPES_PILOT_2026-11-10.json`, `M2_DEV_RUN.json`,
`B3_ROOTCAUSE_2026-11-10.json`, `PERF_V12_*`, `ARCHIVE_NATIVE_MANIFEST.json`,
`ACCEPTANCE_MATRIX.json`, `LOCAL_VIEW_*`, `NATIVE_ARCHIVE_DAY.json`,
`NATIVE_FUNDING_*`, `evidence/research/variants_day/*`. Sealed dates
(2026-03-08…2026-10-31) were not read or rebuilt. `data/native/_dev_w1` verified
unaffected (0/1440 differences vs real mark).

---

## 5. BLOCKED / NOT_RUN

| Item | Status | Exact reason |
|---|---|---|
| `gh pr close 75` | **BLOCKED** | PR is already MERGED; GitHub refuses to close a merged PR. Comment posted instead. Measured −12,335 lines vs main (prompt expected ~11,453). |
| `gh pr ready 41 --undo` | **NOT_RUN** | Already a draft; command was unnecessary. Comment posted. |
| Re-merge #93 / #95 | **NOT_RUN** | Already merged before the run; verified their diffs instead. |
| Close #50 / #53 | **NOT_RUN** | Already closed by the pre-run batch; verified comments. |
| TA backlog issue before closing TA issues | **NOT_RUN (ordering)** | TA issues were closed by the pre-run batch; #96 was created after the fact and lists them. No re-open. |
| Full A3 run, B6 calibration, B4 100M | **NOT_RUN** | Forbidden by the run rules. |
| Rebuild `_fold` + 2026-11 dev tapes | **NOT_RUN** | Out of the #94 change scope; they are marked stale, not regenerated. |
| Repoint `scripts/validate_provenance.py` | **NOT_RUN** | The 2026-11-07 day has not been rebuilt; documented as a next step. |
| `systemctl --user is-active qel-soak` | PASS (read-only) | See last check below. |

No command failed three times. No step looped.

---

## 6. Three next steps for the human owner

1. **Rebuild the affected tapes and re-run dependent evidence.** Use the fixed
   builders on `_fold/2024-09-01…07` and `DS-ARCHIVE-BTCUSDT-2026-11-01…07`,
   then re-run PERF v1.2, B3 and `variants_day`. Until then, treat every result
   listed in `docs/EVIDENCE_INVALIDATION_94.md` as mark-stale.
2. **Make the provenance permanent.** Point `scripts/validate_provenance.py` at
   `BTCUSDT-markPriceKlines-1m-<day>.zip`, and add a check that rejects any tape
   manifest claiming `observed_mark_price_kline_close` whose `mark_klines` hash
   equals a trade-kline archive.
3. **Decide the research direction before unfreezing gates.** The survey says
   the naive funding-carry hypothesis is negative after costs (−3.86 USDT/week
   on 1000 notional, screening only). Do not unfreeze #7/#8/#9/#14/#16/#17/#18
   until a development-stage candidate actually exists.

Last check: `git status` clean; no service touched; only the read-only
`systemctl --user is-active qel-soak` was used.
