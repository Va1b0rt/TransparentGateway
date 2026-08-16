"""Validate candidates supplied by configured, authorised connectors."""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

from connectors import ProxyCandidate, collect_all, load_connectors

SNAPSHOT_KEY = "transparent-gateway:validated:v1"


async def probe(candidate: ProxyCandidate, timeout: float) -> dict[str, object] | None:
    host, port_text = candidate.endpoint.rsplit(":", 1)
    started = time.monotonic()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, int(port_text)), timeout)
        writer.close()
        await writer.wait_closed()
        return candidate.snapshot(max(1, round((time.monotonic() - started) * 1000)))
    except (OSError, asyncio.TimeoutError):
        return None


async def cycle(client: Any) -> None:
    candidates = await collect_all(load_connectors(os.getenv("CONNECTORS_CONFIG", "/inventory/connectors.json")))
    semaphore = asyncio.Semaphore(int(os.getenv("VALIDATION_CONCURRENCY", "100")))
    async def guarded(candidate: ProxyCandidate):
        async with semaphore:
            return await probe(candidate, float(os.getenv("PROXY_TCP_TIMEOUT_SECONDS", "3")))
    checked = await asyncio.gather(*(guarded(candidate) for candidate in candidates))
    entries = [result for result in checked if result is not None]
    reserve = int(os.getenv("RESERVE_SIZE", "700"))
    snapshot = {"generated_at": int(time.time()), "entries": sorted(entries, key=lambda item: item["latency_ms"])[:reserve]}
    await client.set(SNAPSHOT_KEY, json.dumps(snapshot, separators=(",", ":")))
    print(f"[validator] healthy={len(entries)}/{len(candidates)}", flush=True)


async def main() -> None:
    import redis.asyncio as redis
    client = redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"), decode_responses=True)
    try:
        while True:
            try:
                await cycle(client)
            except (OSError, ValueError, json.JSONDecodeError) as error:
                print(f"[validator] cycle failed: {error}", flush=True)
            await asyncio.sleep(int(os.getenv("VALIDATION_INTERVAL_SECONDS", "300")))
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
