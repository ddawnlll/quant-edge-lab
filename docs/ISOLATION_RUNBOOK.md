# Isolation runbook (TASK-15 / issue #8)

Status: BLOCKED on administrator-created principals. The software path is
tested; the operator path below is the exact resume action. No UID, socket or
network isolation is claimed until `tests/test_isolation_real.py` passes with
`QEL_REAL_PRINCIPALS=1`.

## Why this is blocked now

The current account is unprivileged (`uid=1000`), has no passwordless sudo, and
the host has no `newuidmap`/multi-UID user-namespace mapping. Two user
namespaces would both map to the same outer UID, so they are not distinct OS
principals. The evaluator therefore runs with mode-only separation
(`0o700` root, `0o600` key/db) and records `T-ISOLATION: BLOCKED`.

## Operator steps

1. As root, run:

   ```sh
   sudo ./scripts/provision_isolation.sh
   ```

   The script creates `qel-research`, `qel-eval` and the `qel-eval-socket`
   group; seals `runtime/` and the evaluator key/db to `qel-eval`; and sets the
   authorized socket to `qel-eval:qel-eval-socket` mode `0660`. It is
   idempotent.

2. Restrict the worker to AF_UNIX only so it has no network egress:

   ```sh
   sudo systemctl edit qel-worker.service
   # [Service]
   # PrivateNetwork=yes
   # RestrictAddressFamilies=AF_UNIX
   sudo systemctl daemon-reload
   sudo systemctl restart qel-worker.service
   ```

3. Run the real-principal acceptance:

   ```sh
   QEL_REAL_PRINCIPALS=1 QEL_SEALED_ROOT=$PWD/runtime/sealed \
     PYTHONPATH=python .venv/bin/python -m pytest -q tests/test_isolation_real.py
   ```

4. Attach the output, `id` of both principals, `namei -l runtime/sealed`,
   `stat` of the key/db/socket, `ss -xl`, and the worker cgroup network
   namespace evidence to issue #8.

## Acceptance mapping

| Requirement | Check |
| --- | --- |
| Distinct research/evaluator principals | `test_principals_are_distinct_non_root` |
| Research cannot read evaluator key/db | `test_research_cannot_read_evaluator_key_or_db` |
| Socket peer ownership | `test_evaluator_owns_socket_and_peer_credentials_are_distinct` |
| Restricted worker network | `test_worker_network_namespace_blocks_external_egress` |
| Durable pre-access receipt | `tests/test_isolation_anchor.py` (already PASS software-only) |

Until then, issue #8 stays open and the PR must not claim `T-ISOLATION`.
