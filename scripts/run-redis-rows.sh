#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

redis_container=$(docker compose ps -q redis)
if [[ -z "$redis_container" ]]; then
  echo "ERROR: the Redis service is not running" >&2
  exit 1
fi

keys=(
  proxy_pool:raw:v1
  proxy_pool:validated:v1
  proxy_pool:reserve:v1
  proxy_pool:active:v1
  transparent-gateway:validated:v1
)

for key in "${keys[@]}"; do
  value=$(docker exec "$redis_container" redis-cli --raw GET "$key")
  if [[ -z "$value" ]]; then
    printf '%s: MISSING\n' "$key"
    continue
  fi

  count=$(python3 -c '
import json
import sys

document = json.load(sys.stdin)
entries = document.get("entries")
if not isinstance(entries, list):
    raise SystemExit("snapshot entries is not a list")
print(len(entries))
' <<<"$value")
  printf '%s: %s\n' "$key" "$count"
done

echo "Note: transparent-gateway:validated:v1 is the gateway compatibility copy of the active pool."
