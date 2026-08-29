"""Validate raw candidates supplied by the authorised-source fetcher."""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

from asn_resolver import AsnResolver
from common.credentials import CredentialStore
from proxy_probe import CheckTarget, classify_anonymity, direct_observation, proxy_observation
from sources import ProxyCandidate, parse_candidate
from storage import RAW_KEY, VALIDATED_KEY, publish_snapshot, read_snapshot


async def probe(
    candidate: ProxyCandidate,
    target: CheckTarget,
    direct_identities: set[str],
    timeout: float,
    credentials: CredentialStore,
    asn_resolver: AsnResolver,
) -> dict[str, object] | None:
    """Verify the proxy protocol, make an echo request, and reject IP leakage."""
    try:
        auth = credentials.get(candidate.credential_ref)
        observation, latency_ms = await proxy_observation(
            candidate,
            target,
            timeout,
            auth,
        )
        anonymity_level = classify_anonymity(observation, direct_identities)
        if anonymity_level is None:
            return None
    except (OSError, ValueError, asyncio.TimeoutError, asyncio.IncompleteReadError):
        return None
    snapshot = candidate.snapshot(latency_ms, anonymity_level=anonymity_level)
    metadata = dict(snapshot.get("metadata") or {})
    source_asn = metadata.pop("asn", None)
    if source_asn:
        metadata["source_asn"] = str(source_asn)
    snapshot["metadata"] = metadata
    snapshot["exit_ip"] = observation.peer
    snapshot["network"] = asn_resolver.resolve(observation.peer)
    return snapshot


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
    timeout = float(os.getenv("PROXY_VALIDATION_TIMEOUT_SECONDS", "8"))
    if timeout <= 0:
        raise ValueError("PROXY_VALIDATION_TIMEOUT_SECONDS must be positive")
    target = CheckTarget.parse(os.getenv("ANONYMITY_CHECK_URL", ""))
    baseline = await direct_observation(target, timeout)
    direct_identities = {baseline.peer}
    direct_identities.update(
        value.strip()
        for value in os.getenv("ANONYMITY_CLIENT_IPS", "").split(",")
        if value.strip()
    )

    credentials = CredentialStore(
        os.getenv("PROXY_CREDENTIALS_FILE", "/run/secrets/proxy-credentials.json")
    )
    with AsnResolver.from_environment() as asn_resolver:
        async def guarded(candidate: ProxyCandidate) -> dict[str, object] | None:
            async with semaphore:
                return await probe(
                    candidate,
                    target,
                    direct_identities,
                    timeout,
                    credentials,
                    asn_resolver,
                )

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
