#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

HAPROXY_SOCKET=/run/haproxy/admin.sock
PROXY_URL=http://10.10.10.2:3129
ECHO_URL=http://10.10.10.2:8000/

if [[ ! -S "$HAPROXY_SOCKET" ]]; then
  echo "ERROR: HAProxy admin socket is unavailable: $HAPROXY_SOCKET" >&2
  exit 1
fi

stats=$(printf 'show stat\n' | socat stdio "$HAPROXY_SOCKET")
down_count=$(awk -F, '
  NR == 1 {for (i=1; i<=NF; i++) if ($i=="status") status=i; next}
  $1=="proxy_pool" && $2!="BACKEND" && $status=="DOWN" {count++}
  END {print count+0}
' <<<"$stats")

if (( down_count == 0 )); then
  echo "ERROR: this check requires at least one DOWN server in proxy_pool" >&2
  exit 1
fi

echo "=== HAPROXY DOWN-BACKEND CHECK ==="
echo "HAProxy backend status:"
awk -F, '
  NR == 1 {for (i=1; i<=NF; i++) if ($i=="status") status=i; next}
  $1=="proxy_pool" {print $2 ": " $status}
' <<<"$stats"

echo
echo "Ten fresh requests to the internal echo server:"

for n in $(seq 1 10); do
  response=$(curl -sS --fail-with-body --max-time 5 \
    --proxy "$PROXY_URL" \
    --noproxy "" \
    -H "Connection: close" \
    -w $'\n%{http_code}' \
    "$ECHO_URL")
  status=${response##*$'\n'}
  body=${response%$'\n'*}
  if [[ "$status" != "200" ]]; then
    echo "ERROR: request $n returned HTTP $status" >&2
    exit 1
  fi
  peer=$(printf "%s" "$body" |
    python3 -c 'import json,sys; print(json.load(sys.stdin).get("backend_peer","unknown"))')
  if [[ "$peer" == "unknown" ]]; then
    echo "ERROR: request $n did not return backend_peer" >&2
    exit 1
  fi
  printf 'request-%02d: HTTP %s backend_peer=%s\n' "$n" "$status" "$peer"
done

echo
echo "=== END ==="
