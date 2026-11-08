#!/usr/bin/env bash
# Durable 24h observation sampler. It records only measured state.
set -u
ROOT="${1:-runtime}"
OUT="$ROOT/soak"
mkdir -p "$OUT"
echo $$ > "$OUT/soak.pid"
START=$(date +%s)
while :; do
  NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  HEALTH=$(curl -s -m 3 http://127.0.0.1:8787/healthz || echo '{"status":"DOWN"}')
  DASH=$(curl -s -m 3 http://127.0.0.1:8787/v1/dashboard || echo '{"state":"DOWN"}')
  MEM=$(cat /sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/qel/soak/memory.current 2>/dev/null || echo 0)
  DISK=$(df -B1 --output=avail /home/quant/quant | tail -1 | tr -d ' ')
  ELAPSED=$(( $(date +%s) - START ))
  printf '{"ts":"%s","elapsed_s":%s,"health":%s,"dashboard":%s,"lab_memory_current":%s,"disk_free_bytes":%s}\n' \
    "$NOW" "$ELAPSED" "$HEALTH" "$DASH" "${MEM:-0}" "${DISK:-0}" >> "$OUT/soak.jsonl"
  sleep 60
done
