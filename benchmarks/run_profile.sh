#!/usr/bin/env bash
# Run one benchmark process under an enforced cgroup v2 memory limit.
#
# usage: run_profile.sh <unit> <mem> <peak-file> -- <command...>
#
# The child runs inside a transient user scope whose memory.max is the declared
# profile limit and whose memory.swap.max is 0. memory.peak is read from the
# same cgroup after the child exits, so the recorded peak is measured under the
# limit rather than inferred from /proc. If delegation is unavailable the
# command fails; no fallback pretends the profile ran.
set -u

unit=$1
mem=$2
peak=$3
shift 3
if [ "${1:-}" = "--" ]; then shift; fi

inner='rc=0; "$@" || rc=$?; p=$(cut -d: -f3 /proc/self/cgroup); cat /sys/fs/cgroup$p/memory.peak > '"$peak"'; exit $rc'

exec systemd-run --user --scope \
  -p "MemoryMax=${mem}" \
  -p "MemorySwapMax=0" \
  --unit "${unit}" \
  --quiet \
  -- sh -c "$inner" sh "$@"
