"""Explicit allow-list registry and loader for source connectors."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Callable, Mapping

from .base import ProxyCandidate, ProxySourceConnector

ConnectorFactory = Callable[[Mapping[str, object]], ProxySourceConnector]


class ConnectorRegistry:
    """A controlled registry: importing a file never enables it automatically."""

    def __init__(self) -> None:
        self._factories: dict[str, ConnectorFactory] = {}

    @property
    def types(self) -> frozenset[str]:
        return frozenset(self._factories)

    def register(self, kind: str, factory: ConnectorFactory) -> None:
        if not kind or kind in self._factories:
            raise ValueError(f"duplicate or empty connector type: {kind}")
        self._factories[kind] = factory

    def create(self, kind: str, config: Mapping[str, object]) -> ProxySourceConnector:
        try:
            factory = self._factories[kind]
        except KeyError:
            raise ValueError(f"connector type is not registered: {kind}") from None
        return factory(config)


def load_connectors(path: str, registry: ConnectorRegistry) -> list[ProxySourceConnector]:
    config = json.loads(Path(path).read_text())
    if not isinstance(config, dict):
        raise ValueError("connector configuration must be an object")
    allowed = config.get("allowed_connector_types")
    active = config.get("active_connectors")
    if not isinstance(allowed, list) or not all(isinstance(kind, str) for kind in allowed):
        raise ValueError("allowed_connector_types must be a list of strings")
    if not isinstance(active, list):
        raise ValueError("active_connectors must be a list")
    allowed_types = set(allowed)
    unknown = allowed_types - registry.types
    if unknown:
        raise ValueError(f"allowed connector types are not registered: {', '.join(sorted(unknown))}")

    connectors: list[ProxySourceConnector] = []
    names: set[str] = set()
    for item in active:
        if not isinstance(item, dict):
            raise ValueError("active connector entry must be an object")
        kind = str(item.get("type") or "")
        name = str(item.get("name") or "")
        if kind not in allowed_types:
            raise ValueError(f"active connector type is not allowed: {kind}")
        if not name or name in names:
            raise ValueError(f"connector name is empty or duplicated: {name}")
        names.add(name)
        connectors.append(registry.create(kind, item))
    return connectors


async def collect_all(connectors: list[ProxySourceConnector]) -> list[ProxyCandidate]:
    async def one(connector: ProxySourceConnector) -> list[ProxyCandidate]:
        return [candidate async for candidate in connector.collect()]

    collected = await asyncio.gather(*(one(connector) for connector in connectors), return_exceptions=True)
    unique: dict[tuple[str, str, int, str | None], ProxyCandidate] = {}
    for connector, candidates in zip(connectors, collected):
        if isinstance(candidates, Exception):
            print(f"[connector:{connector.name}] collection failed: {type(candidates).__name__}", flush=True)
            continue
        for candidate in candidates:
            unique[(candidate.protocol, candidate.host, candidate.port, candidate.credential_ref)] = candidate
    return list(unique.values())
