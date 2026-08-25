"""Connector for an operator-authorised HTTPS JSON feed."""
from __future__ import annotations

import asyncio
import json
import os
import urllib.request
from typing import AsyncIterator, Mapping

from .base import ProxyCandidate, parse_candidate

MAX_FEED_BYTES = 5 * 1024 * 1024


class HttpJsonFeedConnector:
    def __init__(self, name: str, url: str, token_env: str | None = None) -> None:
        if not name or not url.startswith("https://"):
            raise ValueError("http_json_feed requires name and an HTTPS URL")
        self.name, self.url, self.token_env = name, url, token_env

    @classmethod
    def from_config(cls, config: Mapping[str, object]) -> "HttpJsonFeedConnector":
        token_env = str(config["token_env"]) if config.get("token_env") else None
        return cls(str(config.get("name") or ""), str(config.get("url") or ""), token_env)

    def _fetch(self) -> list[object]:
        headers = {"Accept": "application/json", "User-Agent": "transparent-gateway/1"}
        if self.token_env:
            token = os.getenv(self.token_env)
            if not token:
                raise ValueError(f"missing connector token environment variable: {self.token_env}")
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(self.url, headers=headers)
        with urllib.request.urlopen(request, timeout=15) as response:
            if int(response.headers.get("Content-Length") or 0) > MAX_FEED_BYTES:
                raise ValueError("provider feed exceeds 5 MB limit")
            body = response.read(MAX_FEED_BYTES + 1)
        if len(body) > MAX_FEED_BYTES:
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
