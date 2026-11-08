# Quant Edge Lab

Implementation of `QUANT_EDGE_LAB_SPECIFICATION_v1_1` on a single Linux host.
The canonical machine contract is vendored unmodified at
`specification/spec/architecture.json`; the human monograph and baseline
monograph are preserved beside it.

## Hard boundaries

- No real-money orders, paid subscriptions or new paid research calls.
- Sealed OOS data is never read before its gate, receipt and authorization.
- Fixture or schema conformance is not a profitability claim.
- Positive scientific promotion requires a frozen cohort and the evaluator;
  a development run can only produce `NO_EDGE`, `INSUFFICIENT_EVIDENCE` or a
  blocked state.

## Current status

| Area | State |
| --- | --- |
| Specification package | `PASS_SPECIFICATION_ONLY`, MANIFEST verified |
| TASK-01 contract SDK | Python/Go/Rust canonical validators, run-key vectors, F13/F21 |
| TASK-02 hand oracle | F01-F35 decoded, 44/44 scenarios pass, mutation controls pass |
| TASK-08 native feasibility | Pure-Rust Nautilus spike; lock evidence under `locks/` |
| Ledger / queue / data / accounting | see `TASKS.md` |

Detailed state: `STATE.json`, `TASKS.md`, `RESUME.md`, `MORNING_REPORT.md`.

## Build and verify

```sh
make setup          # project Python venv
make contracts      # Go and Rust contract CLIs into bin/
make test           # focused unit and conformance tests
make evidence       # fixture, mutation and cross-language reports
```

## Layout

| Path | Purpose |
| --- | --- |
| `specification/` | Immutable v1.1 package (schemas, fixtures, examples, monograph) |
| `python/qel/contract/` | Canonical JSON, decimal domains, run keys, schema validation |
| `python/qel/conformance/` | QEL_FIXTURE_DSL_1 translator and deterministic hand oracle |
| `contracts/go/` | Go contract validator and fixture-independent canonical CLI |
| `contracts/rust/` | Rust contract validator (policy/native crates build on the same rules) |
| `native/` | Nautilus feasibility spike and native adapter |
| `locks/` | RuntimeLock, source pins, toolchain evidence |
| `evidence/` | Test reports, manifests and measured resource traces |

## Ponytail guidance

Project guidance follows the Ponytail full mode and review/audit skills
from <https://github.com/dietrichgebert/ponytail> at commit
`96608e078b2d5ea607c5c3b8b5a54d1c1fba6846` (MIT). Markdown instructions are
applied directly; no Claude-specific command loader is assumed.
