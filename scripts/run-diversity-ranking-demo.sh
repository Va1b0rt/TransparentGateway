#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

ranker_container=$(docker compose ps -q ranker)
if [[ -z "$ranker_container" ]]; then
  echo "ERROR: the ranker service is not running" >&2
  exit 1
fi

cat <<'PY' | docker exec -i -e PYTHONPATH=/app "$ranker_container" python -
from ranking import rank_entries


def candidate(label, ip, latency, asn):
    return {
        "label": label,
        "endpoint": f"{ip}:3128",
        "address": f"http://{ip}:3128",
        "exit_ip": ip,
        "latency_ms": latency,
        "success_streak": 1,
        "network": {
            "asn_status": "verified",
            "asn": asn,
            "prefix": None,
        },
    }


# Documentation-only addresses keep this demo isolated from the live pool.
# proxy-1 and proxy-2 deliberately share both AS1 and 192.0.2.0/24, so the
# final score demonstrates the combined ASN and subnet repetition penalty.
entries = [
    candidate("proxy-1", "192.0.2.1", 10, "AS1"),
    candidate("proxy-2", "192.0.2.2", 11, "AS1"),
    candidate("proxy-3", "198.51.100.1", 20, "AS2"),
]

print("=== SYNTHETIC ASN/SUBNET DIVERSITY DEMO ===")
print("INPUT")
for item in entries:
    print(
        f"{item['label']}: "
        f"ASN={item['network']['asn']}, "
        f"latency={item['latency_ms']}ms"
    )

print("OUTPUT")
for index, item in enumerate(rank_entries(entries, reserve_size=3), 1):
    print(
        f"{index}. {item['label']}: "
        f"ASN={item['network']['asn']}, "
        f"latency={item['latency_ms']}ms, "
        f"base_score={item['base_score']}, "
        f"score={item['score']}"
    )
print("=== END ===")
PY
