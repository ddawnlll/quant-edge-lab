#!/usr/bin/env bash
# QEL service control. Reproducible start/stop/restart/status.
set -euo pipefail
cmd="${1:-status}"
case "$cmd" in
  start)
    systemctl --user start qel-qeld qel-worker qel-soak
    ;;
  stop)
    systemctl --user stop qel-soak qel-worker qel-qeld
    ;;
  restart)
    systemctl --user restart qel-qeld
    systemctl --user restart qel-worker
    systemctl --user restart qel-soak
    ;;
  status)
    systemctl --user --no-pager status qel-qeld qel-worker qel-soak | grep -E "Active|Main PID|●"
    curl -s -m 3 http://127.0.0.1:8787/healthz || true
    echo
    ;;
  *)
    echo "usage: $0 start|stop|restart|status" >&2
    exit 2
    ;;
esac
