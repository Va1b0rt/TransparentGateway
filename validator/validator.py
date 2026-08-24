"""Validate raw candidates supplied by the authorised-source fetcher."""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

from connectors import ProxyCandidate, parse_candidate
from storage import RAW_KEY, VALIDATED_KEY, publish_snapshot, read_snapshot


async def probe(candidate: ProxyCandidate, timeout: float) -> dict[str, object] | None:
    """Measure endpoint TCP reachability without exposing proxy credentials."""
    host, port_text = candidate.endpoint.rsplit(":", 1)
    started = time.monotonic()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, int(port_text)), timeout)
        writer.close()
        await writer.wait_closed()
        latency_ms = max(1, round((time.monotonic() - started) * 1000))
        return candidate.snapshot(latency_ms)
    except (OSError, asyncio.TimeoutError):
        return None


async def cycle(client: Any) -> tuple[int, int] | None:
    raw_snapshot = await read_snapshot(client, RAW_KEY)
    if raw_snapshot is None:
        print("[validator] waiting for raw snapshot", flush=True)
        return None

    candidates: list[ProxyCandidate] = []
    for value in raw_snapshot["entries"]:
        if not isinstance(value, dict):
            continue
        try:
            candidates.append(parse_candidate(value, "fetcher"))
        except ValueError as error:
            print(f"[validator] skipped raw candidate: {error}", flush=True)

    previous_snapshot = await read_snapshot(client, VALIDATED_KEY)
    previous_streaks = {
        str(entry.get("address")): int(entry.get("success_streak", 0))
        for entry in (previous_snapshot or {}).get("entries", [])
        if isinstance(entry, dict)
    }

    concurrency = int(os.getenv("VALIDATION_CONCURRENCY", "100"))
    if concurrency < 1:
        raise ValueError("VALIDATION_CONCURRENCY must be positive")
    semaphore = asyncio.Semaphore(concurrency)

    async def guarded(candidate: ProxyCandidate) -> dict[str, object] | None:
        async with semaphore:
            return await probe(candidate, float(os.getenv("PROXY_TCP_TIMEOUT_SECONDS", "3")))

    checked = await asyncio.gather(*(guarded(candidate) for candidate in candidates))
    entries: list[dict[str, object]] = []
    for result in checked:
        if result is None:
            continue
        address = str(result["address"])
        result["success_streak"] = previous_streaks.get(address, 0) + 1
        entries.append(result)

    snapshot = {
        "generated_at": int(time.time()),
        "entries": entries,
        "source_generated_at": raw_snapshot.get("generated_at"),
    }
    await publish_snapshot(client, VALIDATED_KEY, snapshot)
    print(f"[validator] healthy={len(entries)}/{len(candidates)}", flush=True)
    return len(entries), len(candidates)


async def main() -> None:
    import redis.asyncio as redis

    client = redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"), decode_responses=True)
    try:
        while True:
            delay = int(os.getenv("VALIDATION_INTERVAL_SECONDS", "300"))
            try:
                if await cycle(client) is None:
                    delay = min(delay, 5)
            except (OSError, ValueError, json.JSONDecodeError) as error:
                print(f"[validator] cycle failed: {type(error).__name__}: {error}", flush=True)
            await asyncio.sleep(delay)
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
