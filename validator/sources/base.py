"""Shared source-connector contract and canonical proxy candidate model."""
from __future__ import annotations

import ipaddress
from dataclasses import asdict, dataclass, field
from typing import AsyncIterator, Mapping, Protocol

ALLOWED_PROTOCOLS = frozenset({"http", "https", "socks4", "socks5"})


@dataclass(frozen=True)
class ProxyCandidate:
    """A normalised proxy candidate. Credentials are referenced, never embedded."""

    host: str
    port: int
    protocol: str
    source: str
    credential_ref: str | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)

    @property
    def endpoint(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}"

    @property
    def address(self) -> str:
        return f"{self.protocol}://{self.endpoint}"

    def snapshot_without_validation(self) -> dict[str, object]:
        value = asdict(self)
        value.update(endpoint=self.endpoint, address=self.address)
        return value

    def snapshot(self, latency_ms: int, *, anonymity_level: str) -> dict[str, object]:
        return {
            **self.snapshot_without_validation(),
            "latency_ms": latency_ms,
            "anonymous": True,
            "anonymity_level": anonymity_level,
        }


class ProxySourceConnector(Protocol):
    name: str

    async def collect(self) -> AsyncIterator[ProxyCandidate]: ...


def _split_endpoint(endpoint: str) -> tuple[str, int]:
    if endpoint.startswith("["):
        closing = endpoint.find("]")
        if closing < 0 or closing + 1 >= len(endpoint) or endpoint[closing + 1] != ":":
            raise ValueError("invalid bracketed IPv6 endpoint")
        host, port_text = endpoint[1:closing], endpoint[closing + 2:]
    else:
        if endpoint.count(":") != 1:
            raise ValueError("endpoint must be hostname:port, IPv4:port, or [IPv6]:port")
        host, port_text = endpoint.rsplit(":", 1)
    try:
        port = int(port_text)
    except ValueError:
        raise ValueError("endpoint port must be an integer") from None
    return host, port


def _normalise_host(host: str) -> str:
    value = host.strip().lower()
    if not value or any(char.isspace() for char in value):
        raise ValueError("invalid proxy host")
    try:
        return ipaddress.ip_address(value).compressed
    except ValueError:
        try:
            ascii_host = value.encode("idna").decode("ascii")
        except UnicodeError:
            raise ValueError("invalid proxy hostname") from None
        if len(ascii_host) > 253 or not all(
            label and len(label) <= 63 and label.replace("-", "").isalnum()
            and not label.startswith("-") and not label.endswith("-")
            for label in ascii_host.rstrip(".").split(".")
        ):
            raise ValueError("invalid proxy hostname")
        return ascii_host.rstrip(".")


def parse_candidate(value: Mapping[str, object], default_source: str) -> ProxyCandidate:
    """Accept the canonical host/port form and the legacy endpoint form."""

    raw_endpoint = str(value.get("endpoint") or "").strip()
    raw_host = str(value.get("host") or "").strip()
    raw_port = value.get("port")
    if raw_endpoint:
        endpoint_host, endpoint_port = _split_endpoint(raw_endpoint)
        if raw_host and _normalise_host(raw_host) != _normalise_host(endpoint_host):
            raise ValueError("host conflicts with endpoint")
        if raw_port is not None:
            try:
                configured_port = int(raw_port)
            except (TypeError, ValueError):
                raise ValueError("port must be an integer") from None
            if configured_port != endpoint_port:
                raise ValueError("port conflicts with endpoint")
        host, port = endpoint_host, endpoint_port
    elif raw_host and raw_port is not None:
        host = raw_host
        try:
            port = int(raw_port)
        except (TypeError, ValueError):
            raise ValueError("port must be an integer") from None
    else:
        raise ValueError("candidate requires host and port (or endpoint)")

    protocol = str(value.get("protocol") or "").strip().lower()
    source = str(value.get("source") or default_source).strip()
    credential_ref = value.get("credential_ref")
    metadata = value.get("metadata") or {}
    if protocol not in ALLOWED_PROTOCOLS or not source:
        raise ValueError("candidate requires an allowed protocol and source")
    host = _normalise_host(host)
    if not 1 <= port <= 65535:
        raise ValueError("proxy port is outside 1..65535")
    if credential_ref is not None and not str(credential_ref).strip():
        raise ValueError("credential_ref cannot be blank")
    if not isinstance(metadata, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in metadata.items()
    ):
        raise ValueError("metadata must be a string map")
    return ProxyCandidate(
        host=host,
        port=port,
        protocol=protocol,
        source=source,
        credential_ref=str(credential_ref) if credential_ref else None,
        metadata=dict(metadata),
    )
