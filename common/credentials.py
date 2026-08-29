"""Reloadable proxy credential store.

Snapshots and logs carry only ``credential_ref``.  Usernames and passwords are
resolved from this file at the point where a proxy handshake is performed.
"""
from __future__ import annotations

import json
import hashlib
import time
from pathlib import Path
from typing import Callable


class CredentialStore:
    def __init__(
        self,
        path: str,
        *,
        reload_interval_seconds: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if reload_interval_seconds < 0:
            raise ValueError("credential reload interval cannot be negative")
        self.path = Path(path)
        self.reload_interval_seconds = reload_interval_seconds
        self.clock = clock
        self.last_checked = float("-inf")
        self.digest: str | None = None
        self.values: dict[str, object] = {}

    def get(self, reference: str | None) -> tuple[str, str] | None:
        if reference is None:
            return None
        now = self.clock()
        if now - self.last_checked >= self.reload_interval_seconds:
            self._reload()
            self.last_checked = now
        item = self.values.get(reference)
        if not isinstance(item, dict):
            raise ValueError("proxy credential reference is unavailable")
        username, password = item.get("username"), item.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            raise ValueError("proxy credential entry is invalid")
        return username, password

    def _reload(self) -> None:
        try:
            content = self.path.read_bytes()
        except OSError:
            raise ValueError("proxy credential store is unavailable") from None
        digest = hashlib.sha256(content).hexdigest()
        if digest == self.digest:
            return
        try:
            raw = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("proxy credential store is unreadable") from None
        if not isinstance(raw, dict):
            raise ValueError("proxy credential store must be an object")
        self.values, self.digest = raw, digest
