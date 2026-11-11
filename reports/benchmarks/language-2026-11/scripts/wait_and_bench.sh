#!/bin/bash
# Wait for external rustc/cargo/cc1 compilation to drain (quiet), then run L1 matrix once.
set -u
ROOT=/home/quant/quant/bench-lang-2026-11
cd $ROOT
echo "waiter started $(date -Is)"
QUIET=0
for i in $(seq 1 240); do
  N=$(pgrep -c rustc 2>/dev/null || true); N=${N:-0}
  M=$(pgrep -c "cargo build" 2>/dev/null || true); M=${M:-0}
  C=$(pgrep -c cc1 2>/dev/null || true); C=${C:-0}
  if [ "$N" -eq 0 ] && [ "$M" -eq 0 ] && [ "$C" -eq 0 ]; then
    QUIET=$((QUIET+1))
  else
    QUIET=0
  fi
  if [ "$QUIET" -ge 3 ]; then
    echo "quiet confirmed after $i polls ($(date -Is))"
    break
  fi
  sleep 20
done
echo "rustc=$(pgrep -c rustc 2>/dev/null || true) cc1=$(pgrep -c cc1 2>/dev/null || true) load=$(cat /proc/loadavg)"
rm -f raw/trials.jsonl
python3 scripts/bench.py 2>&1 | tee raw/bench.log
echo "bench done $(date -Is)"
