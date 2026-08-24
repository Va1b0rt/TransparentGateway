"""Rank validated candidates and publish reserve and active pools."""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

from ranking import rank_entries
from storage import RESERVE_KEY, VALIDATED_KEY, publish_active_snapshot, publish_snapshot, read_snapshot


def positive_int(name: str, default: str) -> int:
    value = int(os.getenv(name, default))
    if value < 1:
        raise ValueError(f"{name} must be positive")
    return value


async def cycle(client: Any) -> tuple[int, int] | None:
    validated = await read_snapshot(client, VALIDATED_KEY)
    if validated is None:
        print("[ranker] waiting for validated snapshot", flush=True)
        return None

    reserve_size = positive_int("RESERVE_SIZE", "700")
    pool_size = positive_int("POOL_SIZE", "150")
    if pool_size > reserve_size:
        raise ValueError("POOL_SIZE must be less than or equal to RESERVE_SIZE")
    min_score = float(os.getenv("MIN_SCORE_THRESHOLD", "0"))

    entries = [dict(entry) for entry in validated["entries"] if isinstance(entry, dict)]
    ranked = rank_entries(entries, reserve_size=reserve_size, min_score=min_score)
    generated_at = int(time.time())
    reserve_snapshot = {
        "generated_at": generated_at,
        "entries": ranked,
        "min_score_threshold": min_score,
        "reserve_size": reserve_size,
    }
    active_snapshot = {
        "generated_at": generated_at,
        "entries": ranked[:pool_size],
        "min_score_threshold": min_score,
        "pool_size": pool_size,
    }
    await publish_snapshot(client, RESERVE_KEY, reserve_snapshot)
    await publish_active_snapshot(client, active_snapshot)
    print(f"[ranker] validated={len(entries)} reserve={len(ranked)} active={len(active_snapshot['entries'])}", flush=True)
    return len(ranked), len(active_snapshot["entries"])


async def main() -> None:
    import redis.asyncio as redis

    client = redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"), decode_responses=True)
    try:
        while True:
            delay = int(os.getenv("RANK_INTERVAL_SECONDS", "60"))
            try:
                if await cycle(client) is None:
                    delay = min(delay, 5)
            except (OSError, ValueError, json.JSONDecodeError) as error:
                print(f"[ranker] cycle failed: {type(error).__name__}: {error}", flush=True)
            await asyncio.sleep(delay)
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
