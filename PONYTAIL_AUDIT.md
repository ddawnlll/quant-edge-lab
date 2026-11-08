# Ponytail review and audit

Revision: `96608e078b2d5ea607c5c3b8b5a54d1c1fba6846` (MIT). Full mode.
Scope: the whole repository, with deep reading of the money, artifact, queue,
evaluator, worker and cgroup paths. The expected load is one operator and one
heavy worker on a 6-vCPU host; the 100x replay test covers the largest
in-process load.

## What this repo does

It implements the QEL contracts, a hand-oracle conformance harness, an
append-only ledger and artifact store, a durable queue/worker, public-data
capture and normalization, a Rust typed policy, a Nautilus taker runner,
independent accounting and a sealed evaluator. It refuses to promote or trade
without fresh OOS and authorization.

## Must fix (fixed during this pass)

1. **Unauthenticated localhost writes.** `/v1/jobs` and `/v1/dev/params`
   accepted any local process. Fixed with a 48-hex bearer token in
   `runtime/http.token` mode 0600; qeld generates it, qel-submit reads it.
2. **Artifact duplicate-seal deadlock.** A duplicate manifest inserted a
   duplicate row inside an open transaction, then the failure path asked the
   single SQLite connection for another statement. Fixed by returning the
   existing event hash before the transaction and rolling back before failure
   recording.
3. **cgroup descriptor closed too early.** The worker closed the cgroup fd
   before `cmd.Run`, yielding `bad file descriptor`. Fixed by keeping it open
   until the child has started and reaped.
4. **Requeued job kept the old attempt id.** Fixed to `ATT-…-R<generation>`.
5. **Sealed evaluator could overwrite a decision.** Fixed to reject a second
   decision and to fsync the anchor chain.
6. **Runtime permissions.** Runtime root 750, research DB 640, HTTP token 600.

## Should fix (open)

1. OS principal separation is still same-uid. The private DB/key are 0600 and
   separate from the research ledger, but a distinct `qel-eval` owner needs an
   administrator. Do not claim isolation before that exists.
2. B6 is only a bounded AR1 null calibration; heavy-tail, common-factor and
   regime-shift generators are not yet exercised.
3. Matched controls are one random-entry family on the same quote tape; they
   are not yet a full 12-variant control family with identical holding-time
   distributions.
4. M4 remains blocked by the pinned backend's missing LocalView/queue hooks.

## Nice to have

- Remove the unused `Worker.Bin` field and the unused `jobDir` parameter.
- Add a `qel-ledger verify` CLI instead of relying on the Go test for chain
  verification.
- Add an artifact download endpoint that resolves a package index to a file.

## Verdict

Ship the development lab and keep the blocked capabilities blocked. The
critical correctness and exposure findings above are fixed and covered by
tests. No positive edge or trading authorization is claimed.
