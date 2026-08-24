"""Atomic JSON snapshot storage shared by proxy-pool workers."""
from __future__ import annotations

import json
import uuid
from typing import Any

RAW_KEY = "proxy_pool:raw:v1"
VALIDATED_KEY = "proxy_pool:validated:v1"
RESERVE_KEY = "proxy_pool:reserve:v1"
ACTIVE_KEY = "proxy_pool:active:v1"

# Existing gateway releases read this key. Keep publishing it while the
# internal pipeline uses the more explicit proxy_pool:* namespace.
GATEWAY_ACTIVE_KEY = "transparent-gateway:validated:v1"


async def read_snapshot(client: Any, key: str) -> dict[str, object] | None:
    raw = await client.get(key)
    if not raw:
        return None
    value = json.loads(raw)
    if not isinstance(value, dict) or not isinstance(value.get("entries"), list):
        raise ValueError(f"invalid snapshot stored at {key}")
    return value


async def publish_snapshot(client: Any, key: str, snapshot: dict[str, object]) -> None:
    """Publish a complete snapshot without exposing a partially written value."""
    temporary_key = f"{key}:tmp:{uuid.uuid4().hex}"
    encoded = json.dumps(snapshot, separators=(",", ":"), sort_keys=True)
    await client.set(temporary_key, encoded)
    await client.rename(temporary_key, key)


async def publish_active_snapshot(client: Any, snapshot: dict[str, object]) -> None:
    await publish_snapshot(client, ACTIVE_KEY, snapshot)
    await publish_snapshot(client, GATEWAY_ACTIVE_KEY, snapshot)
