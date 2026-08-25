"""Collect candidates from configured, explicitly authorised sources."""
from __future__ import annotations

import asyncio
import json
import os
import time

from sources import CONNECTOR_REGISTRY, collect_all, load_connectors
from storage import RAW_KEY, publish_snapshot


async def cycle(client: object) -> int:
    connectors = load_connectors(
        os.getenv("CONNECTORS_CONFIG", "/inventory/connectors.json"),
        CONNECTOR_REGISTRY,
    )
    candidates = await collect_all(connectors)
    snapshot = {
        "generated_at": int(time.time()),
        "entries": [candidate.snapshot_without_validation() for candidate in candidates],
    }
    await publish_snapshot(client, RAW_KEY, snapshot)
    print(f"[fetcher] sources={len(connectors)} candidates={len(candidates)}", flush=True)
    return len(candidates)


async def main() -> None:
    import redis.asyncio as redis

    client = redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"), decode_responses=True)
    try:
        while True:
            try:
                await cycle(client)
            except (OSError, ValueError, json.JSONDecodeError) as error:
                print(f"[fetcher] cycle failed: {type(error).__name__}: {error}", flush=True)
            await asyncio.sleep(int(os.getenv("FETCH_INTERVAL_SECONDS", "900")))
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
