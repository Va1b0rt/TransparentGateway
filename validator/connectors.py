"""Connector contract and registry for authorised proxy inventories.

Connectors return normalised candidates and never return proxy passwords. A
credential reference is opaque metadata resolved only by the gateway relay.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import AsyncIterator, Protocol

ALLOWED_PROTOCOLS = frozenset({"http", "https", "socks4", "socks5"})


@dataclass(frozen=True)
class ProxyCandidate:
    endpoint: str
    protocol: str
    source: str
    credential_ref: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def address(self) -> str:
        return f"{self.protocol}://{self.endpoint}"

    def snapshot_without_validation(self) -> dict[str, object]:
        return {**asdict(self), "address": self.address}

    def snapshot(self, latency_ms: int) -> dict[str, object]:
        return {**self.snapshot_without_validation(), "latency_ms": latency_ms}


class ProxySourceConnector(Protocol):
    name: str

    async def collect(self) -> AsyncIterator[ProxyCandidate]: ...


def parse_candidate(value: dict[str, object], default_source: str) -> ProxyCandidate:
    endpoint = str(value.get("endpoint") or "").strip().lower()
    protocol = str(value.get("protocol") or "").strip().lower()
    source = str(value.get("source") or default_source).strip()
    credential_ref = value.get("credential_ref")
    metadata = value.get("metadata") or {}
    if protocol not in ALLOWED_PROTOCOLS or not source or not endpoint:
        raise ValueError("candidate requires endpoint, allowed protocol, and source")
    if endpoint.count(":") != 1:
        raise ValueError("endpoint must be IPv4-or-hostname:port")
    host, port_text = endpoint.rsplit(":", 1)
    port = int(port_text)
    if not host or not 1 <= port <= 65535 or any(char.isspace() for char in host):
        raise ValueError("invalid endpoint")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not all(label and label.replace("-", "").isalnum() for label in host.split(".")):
            raise ValueError("invalid endpoint hostname")
    if credential_ref is not None and not str(credential_ref).strip():
        raise ValueError("credential_ref cannot be blank")
    if not isinstance(metadata, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in metadata.items()):
        raise ValueError("metadata must be a string map")
    return ProxyCandidate(endpoint, protocol, source, str(credential_ref) if credential_ref else None, dict(metadata))


class JsonlInventoryConnector:
    """A local, auditable connector suitable for managed providers and CMDB exports."""
    def __init__(self, name: str, path: str) -> None:
        self.name, self.path = name, Path(path)

    async def collect(self) -> AsyncIterator[ProxyCandidate]:
        for line_number, line in enumerate(self.path.read_text().splitlines(), 1):
            content = line.strip()
            if not content or content.startswith("#"):
                continue
            try:
                value = json.loads(content)
                if not isinstance(value, dict):
                    raise ValueError("line is not an object")
                yield parse_candidate(value, self.name)
            except (json.JSONDecodeError, ValueError) as error:
                print(f"[connector:{self.name}] skipped line {line_number}: {error}", flush=True)


class HttpJsonFeedConnector:
    """HTTPS provider-feed connector returning a JSON list of candidates."""
    def __init__(self, name: str, url: str, token_env: str | None = None) -> None:
        if not url.startswith("https://"):
            raise ValueError("http_json_feed requires an HTTPS URL")
        self.name, self.url, self.token_env = name, url, token_env

    def _fetch(self) -> list[object]:
        headers = {"Accept": "application/json", "User-Agent": "transparent-gateway/1"}
        if self.token_env:
            token = os.getenv(self.token_env)
            if not token:
                raise ValueError(f"missing connector token environment variable: {self.token_env}")
            headers["Authorization"] = f"Bearer {token}"
        with urllib.request.urlopen(urllib.request.Request(self.url, headers=headers), timeout=15) as response:
            if int(response.headers.get("Content-Length") or 0) > 5 * 1024 * 1024:
                raise ValueError("provider feed exceeds 5 MB limit")
            body = response.read(5 * 1024 * 1024 + 1)
        if len(body) > 5 * 1024 * 1024:
            raise ValueError("provider feed exceeds 5 MB limit")
        payload = json.loads(body)
        if not isinstance(payload, list):
            raise ValueError("provider feed must return a JSON list")
        return payload

    async def collect(self) -> AsyncIterator[ProxyCandidate]:
        for value in await asyncio.to_thread(self._fetch):
            try:
                if not isinstance(value, dict):
                    raise ValueError("feed item is not an object")
                yield parse_candidate(value, self.name)
            except ValueError as error:
                print(f"[connector:{self.name}] skipped feed item: {error}", flush=True)


CONNECTOR_TYPES = {"jsonl_inventory": JsonlInventoryConnector, "http_json_feed": HttpJsonFeedConnector}


def load_connectors(path: str) -> list[ProxySourceConnector]:
    config = json.loads(Path(path).read_text())
    if not isinstance(config, list):
        raise ValueError("connector configuration must be a list")
    connectors: list[ProxySourceConnector] = []
    for item in config:
        if not isinstance(item, dict):
            raise ValueError("connector entry must be an object")
        if item.get("enabled", True) is False:
            continue
        kind = str(item.get("type") or "")
        name = str(item.get("name") or "")
        factory = CONNECTOR_TYPES.get(kind)
        if factory is None or not name:
            raise ValueError(f"unsupported connector: {kind}")
        if kind == "jsonl_inventory":
            connectors.append(factory(name, str(item.get("path") or "")))
        elif kind == "http_json_feed":
            connectors.append(factory(name, str(item.get("url") or ""), str(item["token_env"]) if item.get("token_env") else None))
    return connectors


async def collect_all(connectors: list[ProxySourceConnector]) -> list[ProxyCandidate]:
    async def one(connector: ProxySourceConnector) -> list[ProxyCandidate]:
        return [candidate async for candidate in connector.collect()]
    collected = await asyncio.gather(*(one(connector) for connector in connectors), return_exceptions=True)
    unique: dict[tuple[str, str, str | None], ProxyCandidate] = {}
    for connector, candidates in zip(connectors, collected):
        if isinstance(candidates, Exception):
            print(f"[connector:{connector.name}] collection failed: {type(candidates).__name__}", flush=True)
            continue
        for candidate in candidates:
            unique[(candidate.protocol, candidate.endpoint, candidate.credential_ref)] = candidate
    return list(unique.values())
