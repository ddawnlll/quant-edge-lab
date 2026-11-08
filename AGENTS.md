# Agent guidance

This repository follows the Ponytail full mode and its review/audit skills:
<https://github.com/dietrichgebert/ponytail> at commit
`96608e078b2d5ea607c5c3b8b5a54d1c1fba6846` (MIT).

Working rules for this project, merged with the laboratory specification:

- Take the smallest complete change. Reuse the contract, ledger, artifact,
  queue and test helpers already present.
- Never weaken accounting, execution fidelity, OOS isolation, trial
  accounting, realistic costs or negative-path checks for speed.
- Keep expected fixture values out of the implementation. The hand oracle is
  an input to comparison, never an execution input.
- A blocked native hook is `BLOCKED_BACKEND`; do not fork, monkey-patch or
  invent an API.
- Run focused tests while editing and the broad suite at integration
  checkpoints. Preserve the original specification package unchanged.
- Do not commit runtime data, SQLite files, keys, private OOS data or build
  caches.
