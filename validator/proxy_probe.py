"""Protocol-aware proxy and anonymity validation.

The check endpoint must return JSON containing ``backend_peer`` (preferred) or
``origin`` plus a ``headers`` object. The repository's test-echo.py implements
that contract. A candidate is accepted only when the endpoint observes a peer
different from the validator's direct peer and no forwarding header leaks that
direct identity.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
import ssl
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import SplitResult, urlsplit

from sources import ProxyCandidate

MAX_HTTP_HEADER_BYTES = 64 * 1024
MAX_HTTP_BODY_BYTES = 256 * 1024
LEAK_HEADERS = frozenset({
    "client-ip",
    "cf-connecting-ip",
    "forwarded",
    "forwarded-for",
    "true-client-ip",
    "x-client-ip",
    "x-cluster-client-ip",
    "x-forwarded-for",
    "x-real-ip",
})
PROXY_MARKER_HEADERS = LEAK_HEADERS | {"proxy-connection", "via", "x-forwarded-proto"}


@dataclass(frozen=True)
class CheckTarget:
    scheme: str
    host: str
    port: int
    path: str

    @classmethod
    def parse(cls, url: str) -> "CheckTarget":
        parsed: SplitResult = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("ANONYMITY_CHECK_URL must be an http:// or https:// URL")
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError("anonymity-check URL cannot contain credentials or a fragment")
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError:
            raise ValueError("anonymity-check URL contains an invalid port") from None
        path = parsed.path or "/"
        if parsed.query:
            path += f"?{parsed.query}"
        return cls(parsed.scheme, parsed.hostname, port, path)

    @property
    def authority(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        default_port = 443 if self.scheme == "https" else 80
        return host if self.port == default_port else f"{host}:{self.port}"

    @property
    def absolute_url(self) -> str:
        return f"{self.scheme}://{self.authority}{self.path}"


@dataclass(frozen=True)
class EchoObservation:
    peer: str
    headers: Mapping[str, str]


async def _read_headers(reader: asyncio.StreamReader) -> tuple[int, dict[str, str], bytes]:
    raw = await reader.readuntil(b"\r\n\r\n")
    if len(raw) > MAX_HTTP_HEADER_BYTES:
        raise OSError("HTTP response headers are too large")
    lines = raw[:-4].split(b"\r\n")
    try:
        status_parts = lines[0].decode("ascii").split(" ", 2)
        status = int(status_parts[1])
    except (IndexError, UnicodeDecodeError, ValueError):
        raise OSError("invalid HTTP response status") from None
    headers: dict[str, str] = {}
    for line in lines[1:]:
        try:
            name, value = line.decode("iso-8859-1").split(":", 1)
        except ValueError:
            raise OSError("invalid HTTP response header") from None
        headers[name.strip().lower()] = value.strip()
    return status, headers, raw


async def _read_chunked(reader: asyncio.StreamReader) -> bytes:
    body = bytearray()
    while True:
        line = await reader.readline()
        try:
            size = int(line.split(b";", 1)[0].strip(), 16)
        except ValueError:
            raise OSError("invalid chunked response") from None
        if size == 0:
            await reader.readuntil(b"\r\n")
            return bytes(body)
        if len(body) + size > MAX_HTTP_BODY_BYTES:
            raise OSError("anonymity-check response is too large")
        body.extend(await reader.readexactly(size))
        if await reader.readexactly(2) != b"\r\n":
            raise OSError("invalid chunk delimiter")


async def _read_http_response(reader: asyncio.StreamReader) -> bytes:
    status, headers, _ = await _read_headers(reader)
    if status != 200:
        raise OSError(f"anonymity-check endpoint returned HTTP {status}")
    if headers.get("transfer-encoding", "").lower() == "chunked":
        return await _read_chunked(reader)
    if "content-length" in headers:
        try:
            length = int(headers["content-length"])
        except ValueError:
            raise OSError("invalid Content-Length") from None
        if not 0 <= length <= MAX_HTTP_BODY_BYTES:
            raise OSError("anonymity-check response is too large")
        return await reader.readexactly(length)
    body = await reader.read(MAX_HTTP_BODY_BYTES + 1)
    if len(body) > MAX_HTTP_BODY_BYTES:
        raise OSError("anonymity-check response is too large")
    return body


def _parse_echo(body: bytes) -> EchoObservation:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise OSError("anonymity-check endpoint did not return JSON") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("headers"), dict):
        raise OSError("anonymity-check JSON requires a headers object")
    peer = str(payload.get("backend_peer") or payload.get("origin") or "").split(",", 1)[0].strip()
    try:
        peer = ipaddress.ip_address(peer).compressed
    except ValueError:
        raise OSError("anonymity-check JSON requires backend_peer or origin IP") from None
    headers = {str(name).lower(): str(value) for name, value in payload["headers"].items()}
    return EchoObservation(peer, headers)


def _request_bytes(target: CheckTarget, *, absolute: bool) -> bytes:
    request_target = target.absolute_url if absolute else target.path
    return (
        f"GET {request_target} HTTP/1.1\r\n"
        f"Host: {target.authority}\r\n"
        "Accept: application/json\r\n"
        "User-Agent: transparent-gateway-validator/1\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")


async def _enable_target_tls(writer: asyncio.StreamWriter, target: CheckTarget) -> None:
    await writer.start_tls(ssl.create_default_context(), server_hostname=target.host)


async def _http_proxy_tunnel(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, target: CheckTarget
) -> None:
    authority = target.authority
    writer.write(
        f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\nConnection: keep-alive\r\n\r\n".encode("ascii")
    )
    await writer.drain()
    status, _, _ = await _read_headers(reader)
    if not 200 <= status < 300:
        raise OSError(f"HTTP proxy rejected CONNECT with status {status}")


async def _socks5_tunnel(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, target: CheckTarget
) -> None:
    writer.write(b"\x05\x01\x00")  # SOCKS5, one method, no authentication.
    await writer.drain()
    response = await reader.readexactly(2)
    if response != b"\x05\x00":
        raise OSError("SOCKS5 proxy does not allow unauthenticated access")
    try:
        packed = socket.inet_pton(socket.AF_INET, target.host)
        address = b"\x01" + packed
    except OSError:
        try:
            packed = socket.inet_pton(socket.AF_INET6, target.host)
            address = b"\x04" + packed
        except OSError:
            hostname = target.host.encode("idna")
            if len(hostname) > 255:
                raise ValueError("anonymity-check hostname is too long")
            address = b"\x03" + bytes([len(hostname)]) + hostname
    writer.write(b"\x05\x01\x00" + address + target.port.to_bytes(2, "big"))
    await writer.drain()
    response = await reader.readexactly(4)
    if response[:2] != b"\x05\x00":
        raise OSError(f"SOCKS5 CONNECT failed with code {response[1]}")
    if response[3] == 1:
        address_size = 4
    elif response[3] == 3:
        address_size = (await reader.readexactly(1))[0]
    elif response[3] == 4:
        address_size = 16
    else:
        raise OSError("invalid SOCKS5 bind address")
    await reader.readexactly(address_size + 2)


async def _socks4_tunnel(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, target: CheckTarget
) -> None:
    try:
        address = socket.inet_aton(target.host)
        suffix = b""
    except OSError:
        address = b"\x00\x00\x00\x01"
        suffix = target.host.encode("idna") + b"\x00"
    writer.write(b"\x04\x01" + target.port.to_bytes(2, "big") + address + b"\x00" + suffix)
    await writer.drain()
    response = await reader.readexactly(8)
    if response[1] != 0x5A:
        raise OSError("SOCKS4 CONNECT rejected")


async def direct_observation(target: CheckTarget, timeout: float) -> EchoObservation:
    async def exchange() -> EchoObservation:
        tls = ssl.create_default_context() if target.scheme == "https" else None
        reader, writer = await asyncio.open_connection(
            target.host, target.port, ssl=tls, server_hostname=target.host if tls else None
        )
        try:
            writer.write(_request_bytes(target, absolute=False))
            await writer.drain()
            return _parse_echo(await _read_http_response(reader))
        finally:
            writer.close()
            await writer.wait_closed()

    return await asyncio.wait_for(exchange(), timeout)


async def proxy_observation(
    candidate: ProxyCandidate, target: CheckTarget, timeout: float
) -> tuple[EchoObservation, int]:
    """Perform a real request through HTTP(S), SOCKS4, or SOCKS5."""
    loop = asyncio.get_running_loop()
    started = loop.time()

    async def exchange() -> EchoObservation:
        proxy_tls = ssl.create_default_context() if candidate.protocol == "https" else None
        reader, writer = await asyncio.open_connection(
            candidate.host,
            candidate.port,
            ssl=proxy_tls,
            server_hostname=candidate.host if proxy_tls else None,
        )
        try:
            absolute_request = candidate.protocol in {"http", "https"} and target.scheme == "http"
            if candidate.protocol in {"http", "https"} and target.scheme == "https":
                await _http_proxy_tunnel(reader, writer, target)
            elif candidate.protocol == "socks5":
                await _socks5_tunnel(reader, writer, target)
            elif candidate.protocol == "socks4":
                await _socks4_tunnel(reader, writer, target)
            elif candidate.protocol not in {"http", "https"}:
                raise ValueError("unsupported proxy protocol")
            if target.scheme == "https":
                await _enable_target_tls(writer, target)
            writer.write(_request_bytes(target, absolute=absolute_request))
            await writer.drain()
            return _parse_echo(await _read_http_response(reader))
        finally:
            writer.close()
            await writer.wait_closed()

    observation = await asyncio.wait_for(exchange(), timeout)
    latency_ms = max(1, round((loop.time() - started) * 1000))
    return observation, latency_ms


def classify_anonymity(observation: EchoObservation, direct_identities: set[str]) -> str | None:
    """Return ``elite``/``anonymous`` or None when the client identity leaked."""
    normalised_identities: set[str] = set()
    for value in direct_identities:
        try:
            normalised_identities.add(ipaddress.ip_address(value.strip()).compressed)
        except ValueError:
            raise ValueError(f"invalid direct client identity: {value}") from None
    if not normalised_identities:
        raise ValueError("at least one direct client identity is required")
    if observation.peer in normalised_identities:
        return None
    for name in LEAK_HEADERS:
        header_value = observation.headers.get(name, "")
        if any(identity in header_value for identity in normalised_identities):
            return None
    return "anonymous" if PROXY_MARKER_HEADERS.intersection(observation.headers) else "elite"
