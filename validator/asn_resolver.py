"""Local ASN enrichment for the exit IP observed by the echo endpoint."""
from __future__ import annotations

import ipaddress
import os
import time
from pathlib import Path
from typing import Any, Callable


class AsnDatabaseError(OSError):
    """The local ASN database cannot safely be used for this cycle."""


class AsnResolver:
    def __init__(
        self,
        path: str,
        max_age_seconds: int,
        *,
        reader_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.path = Path(path)
        self.max_age_seconds = max_age_seconds
        self.reader_factory = reader_factory
        self.reader: Any | None = None

    @classmethod
    def from_environment(cls) -> "AsnResolver":
        max_age = int(os.getenv("ASN_MAX_DATABASE_AGE_SECONDS", "259200"))
        if max_age < 1:
            raise ValueError("ASN_MAX_DATABASE_AGE_SECONDS must be positive")
        return cls(
            os.getenv("ASN_DATABASE_PATH", "/asn-db/origin-asn.mmdb"),
            max_age,
        )

    def __enter__(self) -> "AsnResolver":
        try:
            stat = self.path.stat()
        except OSError:
            raise AsnDatabaseError("ASN database is unavailable") from None
        if time.time() - stat.st_mtime > self.max_age_seconds:
            raise AsnDatabaseError("ASN database is stale")
        if self.reader_factory is None:
            try:
                import maxminddb
            except ImportError:
                raise AsnDatabaseError("maxminddb reader is unavailable") from None
            self.reader_factory = maxminddb.open_database
        try:
            self.reader = self.reader_factory(str(self.path))
        except Exception:
            raise AsnDatabaseError("ASN database cannot be opened") from None
        return self

    def __exit__(self, *_args: object) -> None:
        if self.reader is not None:
            self.reader.close()
            self.reader = None

    def resolve(self, address: str) -> dict[str, object]:
        if self.reader is None:
            raise AsnDatabaseError("ASN resolver is not open")
        try:
            ip = ipaddress.ip_address(address)
            record, prefix_length = self.reader.get_with_prefix_len(ip.compressed)
        except ValueError:
            raise ValueError("echo endpoint returned an invalid exit IP") from None
        except Exception:
            raise AsnDatabaseError("ASN lookup failed") from None
        if not isinstance(record, dict):
            return {
                "asn_status": "unknown",
                "asn": None,
                "organization": None,
                "prefix": None,
            }
        number = record.get("autonomous_system_number")
        organization = record.get("autonomous_system_organization")
        if not isinstance(number, int) or number < 1:
            return {
                "asn_status": "unknown",
                "asn": None,
                "organization": None,
                "prefix": None,
            }
        network = ipaddress.ip_network(f"{ip.compressed}/{prefix_length}", strict=False)
        return {
            "asn_status": "verified",
            "asn": f"AS{number}",
            "organization": str(organization or ""),
            "prefix": str(network),
        }
