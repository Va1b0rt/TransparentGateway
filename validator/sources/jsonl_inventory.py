"""Connector for an operator-managed JSON Lines proxy inventory."""
from __future__ import annotations

import json
from pathlib import Path
from typing import AsyncIterator, Mapping

from .base import ProxyCandidate, parse_candidate


class JsonlInventoryConnector:
    def __init__(self, name: str, path: str) -> None:
        if not name or not path:
            raise ValueError("jsonl_inventory requires name and path")
        self.name, self.path = name, Path(path)

    @classmethod
    def from_config(cls, config: Mapping[str, object]) -> "JsonlInventoryConnector":
        return cls(str(config.get("name") or ""), str(config.get("path") or ""))

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
