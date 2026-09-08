"""Connector for an operator-authorised HTTPS JSON feed."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit
import urllib.request
from typing import AsyncIterator, Mapping

from .base import ProxyCandidate, parse_candidate

MAX_FEED_BYTES = 5 * 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a bearer credential to a redirected destination.
        raise ValueError("provider redirects are disabled")


class HttpJsonFeedConnector:
    def __init__(self, name: str, url: str, token_file: str | None = None) -> None:
        if not name or not url.startswith("https://"):
            raise ValueError("http_json_feed requires name and an HTTPS URL")
        parsed = urlsplit(url)
        if not parsed.hostname or parsed.username or parsed.password or parsed.fragment or parsed.query:
            raise ValueError("provider URL must not contain credentials, query or fragment")
        self.name, self.url, self.token_file = name, url, token_file

    @classmethod
    def from_config(cls, config: Mapping[str, object]) -> "HttpJsonFeedConnector":
        if config.get("token_env"):
            raise ValueError("token_env is unsupported; migrate to token_file")
        token_file = str(config["token_file"]) if config.get("token_file") else None
        return cls(str(config.get("name") or ""), str(config.get("url") or ""), token_file)

    def _fetch(self) -> list[object]:
        headers = {"Accept": "application/json", "User-Agent": "transparent-gateway/1"}
        if self.token_file:
            token = Path(self.token_file).read_text().strip()
            if not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
                raise ValueError("provider token file is empty or invalid")
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(self.url, headers=headers)
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
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
                print(f"[connector:{self.name}] skipped invalid feed item", flush=True)
