"""Periodically download and atomically install the local ASN database."""
from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
import urllib.request
from pathlib import Path
from typing import Callable

DEFAULT_DATABASE_URL = (
    "https://github.com/sapics/ip-location-db/releases/download/latest/"
    "origin-asn.mmdb"
)
DEFAULT_CHECKSUM_URL = (
    "https://github.com/sapics/ip-location-db/releases/download/checksum/"
    "origin-asn.mmdb.sha256"
)
MAX_DATABASE_BYTES = 64 * 1024 * 1024


def _read_url(url: str, limit: int, opener: Callable[..., object]) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "transparent-gateway-asn-updater/1"},
    )
    with opener(request, timeout=60) as response:  # type: ignore[attr-defined]
        body = response.read(limit + 1)  # type: ignore[attr-defined]
    if len(body) > limit:
        raise ValueError("ASN download exceeds the configured size limit")
    return body


def update_once(
    destination: Path,
    database_url: str,
    checksum_url: str,
    *,
    opener: Callable[..., object] = urllib.request.urlopen,
) -> bool:
    checksum_text = _read_url(checksum_url, 4096, opener).decode("ascii")
    expected = checksum_text.split()[0].lower()
    if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
        raise ValueError("ASN checksum response is invalid")
    body = _read_url(database_url, MAX_DATABASE_BYTES, opener)
    actual = hashlib.sha256(body).hexdigest()
    if actual != expected:
        raise ValueError("ASN database checksum mismatch")
    if destination.exists() and hashlib.sha256(destination.read_bytes()).hexdigest() == actual:
        os.utime(destination, None)
        return False

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f"{destination.name}.",
            dir=destination.parent,
            delete=False,
        ) as temporary:
            temporary.write(body)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.chmod(temporary_name, 0o644)
        os.replace(temporary_name, destination)
        temporary_name = None
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return True


async def main() -> None:
    destination = Path(os.getenv("ASN_DATABASE_PATH", "/asn-db/origin-asn.mmdb"))
    database_url = os.getenv("ASN_DATABASE_URL", DEFAULT_DATABASE_URL)
    checksum_url = os.getenv("ASN_CHECKSUM_URL", DEFAULT_CHECKSUM_URL)
    interval = int(os.getenv("ASN_UPDATE_INTERVAL_SECONDS", "86400"))
    if interval < 300:
        raise ValueError("ASN_UPDATE_INTERVAL_SECONDS must be at least 300")
    while True:
        try:
            changed = await asyncio.to_thread(
                update_once,
                destination,
                database_url,
                checksum_url,
            )
            print(
                f"[asn-updater] database={'updated' if changed else 'current'}",
                flush=True,
            )
        except (OSError, UnicodeError, ValueError) as error:
            print(
                f"[asn-updater] update failed: {type(error).__name__}: {error}",
                flush=True,
            )
        await asyncio.sleep(interval)


if __name__ == "__main__":
    asyncio.run(main())
