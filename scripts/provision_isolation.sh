#!/usr/bin/env bash
# Reproducible operator provisioning for TASK-15 / issue #8.
#
# This script requires root. It creates the two distinct principals, the
# evaluator socket group, seals the evaluator root/key/db to qel-eval, and
# prints the exact verification commands. It is idempotent and does not delete
# any existing user, group or file.
#
# Usage:
#   sudo ./scripts/provision_isolation.sh [--root /path/to/qel-runtime]
#
# After it succeeds, run as the operator:
#   QEL_REAL_PRINCIPALS=1 PYTHONPATH=python \
#     python -m pytest -q tests/test_isolation_real.py
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd)
QEL_ROOT=${QEL_ISOLATION_ROOT:-"$ROOT_DIR/runtime"}
SOCKET_DIR=${QEL_SOCKET_DIR:-/run/qel}
SOCKET="$SOCKET_DIR/qel-eval.sock"
RESEARCH_USER=qel-research
EVAL_USER=qel-eval
SOCKET_GROUP=qel-eval-socket

if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: provision_isolation.sh requires root (no passwordless sudo is assumed)." >&2
  exit 77
fi

if [ "${1:-}" = "--root" ]; then
  QEL_ROOT=${2:?--root requires a path}
fi

for user in "$RESEARCH_USER" "$EVAL_USER"; do
  if ! getent passwd "$user" >/dev/null; then
    useradd --system --create-home --shell /usr/sbin/nologin "$user"
    echo "created user $user"
  else
    echo "user $user already exists"
  fi
done

if ! getent group "$SOCKET_GROUP" >/dev/null; then
  groupadd --system "$SOCKET_GROUP"
  echo "created group $SOCKET_GROUP"
fi
usermod -aG "$SOCKET_GROUP" "$RESEARCH_USER"
usermod -aG "$SOCKET_GROUP" "$EVAL_USER"

install -d -m 0700 -o "$EVAL_USER" -g "$EVAL_USER" "$QEL_ROOT"
install -d -m 0750 -o root -g "$EVAL_USER" "$QEL_ROOT/sealed"
install -d -m 0750 -o root -g "$SOCKET_GROUP" "$SOCKET_DIR"

# The sealed evaluator owns its private key, registry and sqlite database.
for path in "$QEL_ROOT/sealed" "$QEL_ROOT/sealed/key.pem" "$QEL_ROOT/sealed/registry.sqlite"; do
  if [ -e "$path" ]; then
    chown "$EVAL_USER:$EVAL_USER" "$path"
    chmod 0600 "$path" 2>/dev/null || chmod 0700 "$path"
  fi
done
if [ -d "$QEL_ROOT/sealed" ]; then
  chmod 0700 "$QEL_ROOT/sealed"
fi

# The authorized socket is owned by the evaluator group; the research worker
# may connect but cannot read the evaluator key or database.
if [ -S "$SOCKET" ]; then
  chown "$EVAL_USER:$SOCKET_GROUP" "$SOCKET"
  chmod 0660 "$SOCKET"
fi

cat <<EOF

Provisioning complete.

Principals:
  $(id "$RESEARCH_USER")
  $(id "$EVAL_USER")

Verification (run as the operator):
  runuser -u $RESEARCH_USER -- id
  runuser -u $EVAL_USER -- id
  namei -l $QEL_ROOT/sealed
  stat -c '%U:%G %a %n' $QEL_ROOT/sealed $QEL_ROOT/sealed/key.pem $QEL_ROOT/sealed/registry.sqlite 2>/dev/null || true
  ss -xl | grep qel-eval || true
  QEL_REAL_PRINCIPALS=1 PYTHONPATH=python python -m pytest -q tests/test_isolation_real.py

Worker network restriction (systemd drop-in, no inbound/outbound network):
  sudo systemctl edit qel-worker.service
  # [Service]
  # PrivateNetwork=yes
  # RestrictAddressFamilies=AF_UNIX
  sudo systemctl daemon-reload && sudo systemctl restart qel-worker.service
EOF
