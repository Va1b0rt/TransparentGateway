import asyncio
import json
import sys
from pathlib import Path

VALIDATOR_DIR = str(Path(__file__).parents[1] / "validator")
sys.path.insert(0, VALIDATOR_DIR)

from ranker import cycle as rank_cycle
from ranking import rank_entries
from storage import ACTIVE_KEY, GATEWAY_ACTIVE_KEY, RESERVE_KEY, VALIDATED_KEY, publish_snapshot


class FakeRedis:
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value):
        self.values[key] = value

    async def rename(self, source, destination):
        self.values[destination] = self.values.pop(source)


def candidate(endpoint, latency_ms, *, asn, success_streak=1):
    return {
        "endpoint": endpoint,
        "protocol": "http",
        "source": "authorised-test",
        "credential_ref": None,
        "metadata": {"asn": asn},
        "address": f"http://{endpoint}",
        "latency_ms": latency_ms,
        "success_streak": success_streak,
    }


def test_ranker_penalises_repeated_asn_and_subnet():
    entries = [
        candidate("10.0.0.1:3128", 10, asn="AS1"),
        candidate("10.0.0.2:3128", 11, asn="AS1"),
        candidate("10.0.1.1:3128", 20, asn="AS2"),
    ]

    ranked = rank_entries(entries, reserve_size=3)

    assert [entry["endpoint"] for entry in ranked] == [
        "10.0.0.1:3128",
        "10.0.1.1:3128",
        "10.0.0.2:3128",
    ]
    assert all("score" in entry and "base_score" in entry for entry in ranked)


def test_ranker_publishes_configurable_active_and_reserve_pools(monkeypatch):
    client = FakeRedis()
    client.values[VALIDATED_KEY] = json.dumps({
        "generated_at": 1,
        "entries": [
            candidate("10.0.0.1:3128", 10, asn="AS1"),
            candidate("10.0.1.1:3128", 20, asn="AS2"),
            candidate("10.0.2.1:3128", 30, asn="AS3"),
        ],
    })
    monkeypatch.setenv("POOL_SIZE", "2")
    monkeypatch.setenv("RESERVE_SIZE", "3")
    monkeypatch.setenv("MIN_SCORE_THRESHOLD", "0")

    reserve_count, active_count = asyncio.run(rank_cycle(client))

    reserve = json.loads(client.values[RESERVE_KEY])
    active = json.loads(client.values[ACTIVE_KEY])
    gateway_active = json.loads(client.values[GATEWAY_ACTIVE_KEY])
    assert (reserve_count, active_count) == (3, 2)
    assert len(reserve["entries"]) == 3
    assert len(active["entries"]) == 2
    assert gateway_active == active


def test_snapshot_publish_uses_atomic_rename():
    client = FakeRedis()

    asyncio.run(publish_snapshot(client, "proxy_pool:test", {"entries": [], "generated_at": 1}))

    assert "proxy_pool:test" in client.values
    assert not any(":tmp:" in key for key in client.values)
