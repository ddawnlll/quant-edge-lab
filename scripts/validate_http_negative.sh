#!/usr/bin/env bash
# Exercise localhost-only binding, write auth, framing and dashboard negatives.
set -u
ROOT=$(cd "$(dirname "$0")/.." && pwd)
BASE=http://127.0.0.1:8787
TOKEN=$(cat "$ROOT/runtime/http.token")
fail=0
check() { if [ "$2" = "$3" ]; then echo "PASS $1=$2"; else echo "FAIL $1=$2 want=$3"; fail=1; fi; }

check health_no_auth "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/healthz")" 200
check jobs_no_token "$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d '{}' "$BASE/v1/jobs")" 401
check jobs_bad_token "$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Authorization: Bearer invalid' -H 'Content-Type: application/json' -d '{}' "$BASE/v1/jobs")" 401
check jobs_malformed "$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{bad' "$BASE/v1/jobs")" 400
check unknown_path "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/v1/not-a-route")" 404
check post_root "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/")" 405
check head_root "$(curl -s -o /dev/null -w '%{http_code}' -I "$BASE/")" 200
check dashboard_csp "$(curl -s -D - -o /dev/null "$BASE/" | tr -d '\r' | awk 'tolower($1)=="content-security-policy:" {print "present"}')" present
check dashboard_nosniff "$(curl -s -D - -o /dev/null "$BASE/" | tr -d '\r' | awk 'tolower($1)=="x-content-type-options:" {print $2}')" nosniff
check listen_localhost "$(ss -ltn | awk '$4=="127.0.0.1:8787" {print "OK"}')" OK
check token_mode "$(stat -c '%a' "$ROOT/runtime/http.token")" 600
check socket_mode "$(stat -c '%a' "$ROOT/runtime/qel.sock")" 660
check db_mode "$(stat -c '%a' "$ROOT/runtime/labd/research.sqlite")" 640
check ledger_verify "$(curl -s "$BASE/v1/ledger/verify")" '{"chain":"verified","status":"OK"}'
exit "$fail"
