# QEL Orchestration Plan — base f985dc6
# Filed: 2026-11-10 (orchestration branch)

## Baseline (verified f985dc6)
- TASK 20/30 DONE strict, 21/30 incl TASK-08 DONE_WITH_BLOCKED
- 6 PARTIAL: 10,15,18,25,26,27 | 3 BLOCKED: 28,29,30
- Fold 1 WF-D64A: train 2024-09-01..2025-08-31 (365d), val 2025-09-01..2025-11-30
- Fold 2 WF-2CCA: train 2024-09-01..2025-11-30 (456d), val 2025-12-01..2026-02-28
- Reserved 237d from 2026-03-08 (SEALED)
- Execution data on disk: 29 daily aggTrades (2026-10 only) + 4 monthly (2024-09, 2026-08..10)
- Monthly aggTrades total 1.7G; daily uncompressed ~12.7MB/day compressed
- Disk free 88G (VM), A2 full daily download estimate ~9.7G for 765d window
- Soak: 24h PASS 2026-11-09, 7-day running to 2026-11-16

## Wave A — M2 unblock (P0)
- A1 (this cycle): PIT symbol feasibility report — denote ELIGIBLE/INFEASIBLE/UNKNOWN
  per symbol using PIT metadata where certified, else UNKNOWN with source gap
- A2 (this cycle): fold coverage audit + resumable ingestion downloader (bounded)
  + pilot validation on 1 day
- A3: blocked until A1 ELIGIBLE + A2 fold coverage COMPLETE

## Wave B (P1)
- B1 #8: audit isolation runbook, do not sudo
- B2 #3: inventory PIT venue sources
- B3 #11: T-QUEUE — do not reimplement F22

## Wave C (P1, resource-serialized)
- C1 #13 B3: needs >=1.70x speedup (203.6s -> <=120s)
- C2 #14/#7/#9 B6: human compute gate only
- C3 #15: soak observation only

## Wave D (P2, offline prep only)
- D1-D3: prepare checklists, do not consume OOS or authorize capital

## Stop conditions (section 6)
No sudo, no sealed-OOS read, no live orders, no threshold relaxation,
no 1187 core-h B6 launch, max 2 workers / 1 heavy job.
