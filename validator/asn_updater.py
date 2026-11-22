"""Periodically download and atomically install the local ASN database."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
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


GITHUB_RELEASE_API = "https://api.github.com/repos/sapics/ip-location-db/releases/tags/latest"


def _asset_digest(body: bytes, opener: Callable[..., object]) -> str:
    """Verify against GitHub's digest for the exact default release asset.

    The separate checksum release can lag behind the database release. Never
    use this fallback for custom sources or accept a locally computed digest
    without matching the independent HTTPS metadata.
    """
    metadata = json.loads(_read_url(GITHUB_RELEASE_API, 2 * 1024 * 1024, opener))
    if not isinstance(metadata, dict) or not isinstance(metadata.get("assets"), list):
        raise ValueError("ASN release metadata is invalid")
    assets = [a for a in metadata["assets"] if isinstance(a, dict)
              and a.get("name") == "origin-asn.mmdb"]
    if len(assets) != 1:
        raise ValueError("ASN release asset missing or ambiguous")
    asset = assets[0]
    digest = asset.get("digest")
    if (asset.get("browser_download_url") != DEFAULT_DATABASE_URL
            or asset.get("state") != "uploaded"
            or type(asset.get("id")) is not int or asset["id"] <= 0
            or type(asset.get("size")) is not int or asset["size"] != len(body)
            or not isinstance(digest, str)
            or not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest)):
        raise ValueError("ASN release asset identity or digest is invalid")
    return digest.split(":", 1)[1].lower()


def update_once(
    destination: Path,
    database_url: str,
    checksum_url: str,
    *,
    opener: Callable[..., object] = urllib.request.urlopen,
) -> bool:
    official_source = database_url == DEFAULT_DATABASE_URL and checksum_url == DEFAULT_CHECKSUM_URL
    expected = None
    try:
        parts = _read_url(checksum_url, 4096, opener).decode("ascii").split()
        if not parts or not re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            raise ValueError("ASN checksum response is invalid")
        expected = parts[0].lower()
    except (OSError, UnicodeError, ValueError):
        if not official_source:
            raise
    body = _read_url(database_url, MAX_DATABASE_BYTES, opener)
    actual = hashlib.sha256(body).hexdigest()
    if actual != expected:
        if not official_source or actual != _asset_digest(body, opener):
            raise ValueError("ASN database checksum mismatch")
        print("[asn-updater] integrity=verified source=github-asset-digest", flush=True)
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
    failures = 0
    while True:
        delay = interval
        try:
            changed = await asyncio.to_thread(
                update_once,
                destination,
                database_url,
                checksum_url,
            )
            failures = 0
            print(
                f"[asn-updater] database={'updated' if changed else 'current'}",
                flush=True,
            )
        except (OSError, UnicodeError, ValueError) as error:
            failures = min(failures + 1, 5)
            delay = min(interval, 3600, 300 * 2 ** (failures - 1))
            # URLs and exception values can include custom-source credentials.
            print(
                f"[asn-updater] update failed: {type(error).__name__}; retry_in={delay}s",
                flush=True,
            )
        await asyncio.sleep(delay)


if __name__ == "__main__":
    asyncio.run(main())
