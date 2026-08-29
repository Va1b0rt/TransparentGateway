"""Local CONNECT relay with per-upstream HTTP/SOCKS authentication.

redsocks speaks HTTP CONNECT to this service. The relay selects a validated
candidate from Redis and establishes the corresponding upstream tunnel without
ever serialising credentials into Redis, HAProxy configuration, or logs.
"""
from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import os
import socket
import ssl
import time
from typing import Any

from common.credentials import CredentialStore

SNAPSHOT_KEY = "transparent-gateway:validated:v1"
DNS_TCP_CAPABLE_KEY = "transparent-gateway:dns-tcp53-capable:v1"
MAX_HEADERS = 16 * 1024


class Pool:
    def __init__(self, redis_url: str, size: int) -> None:
        self.redis_url, self.size, self.entries, self.index, self.last_refresh = redis_url, size, [], 0, 0.0
        self.lock = asyncio.Lock()
        self.failures: dict[tuple[str, int], float] = {}
        self.dns_tcp_capable: set[str] = set()

    @staticmethod
    def key(candidate: dict[str, Any]) -> str:
        return str(candidate.get("address") or f"{candidate.get('protocol')}://{candidate.get('endpoint')}")

    async def choose(self, target_port: int, excluded: set[str] | None = None) -> dict[str, Any] | None:
        import redis.asyncio as redis
        async with self.lock:
            if time.monotonic() - self.last_refresh >= 3:
                self.last_refresh = time.monotonic()
                client = redis.from_url(self.redis_url, decode_responses=True)
                try:
                    raw, dns_capable = await asyncio.gather(
                        client.get(SNAPSHOT_KEY), client.get(DNS_TCP_CAPABLE_KEY)
                    )
                    if raw:
                        parsed = json.loads(raw).get("entries", [])
                        if isinstance(parsed, list):
                            self.entries = parsed[:self.size]
                    if dns_capable:
                        parsed_capable = json.loads(dns_capable)
                        if isinstance(parsed_capable, list):
                            self.dns_tcp_capable = {str(item) for item in parsed_capable}
                except Exception as error:
                    print(f"[egress-relay] Redis unavailable: {type(error).__name__}", flush=True)
                finally:
                    await client.aclose()

            now = time.monotonic()
            excluded = excluded or set()
            candidates = [
                entry for entry in self.entries
                if isinstance(entry, dict)
                and self.key(entry) not in excluded
                and self.failures.get((self.key(entry), target_port), 0.0) <= now
            ]
            if target_port == 53:
                candidates.sort(key=lambda entry: self.key(entry) not in self.dns_tcp_capable)
            if not candidates:
                return None
            selected = candidates[self.index % len(candidates)]
            self.index += 1
            return selected

    async def record_result(self, candidate: dict[str, Any], target_port: int, success: bool) -> None:
        async with self.lock:
            key = self.key(candidate)
            if success:
                self.failures.pop((key, target_port), None)
                if target_port == 53:
                    self.dns_tcp_capable.add(key)
            else:
                self.failures[(key, target_port)] = time.monotonic() + float(
                    os.getenv("UPSTREAM_FAILURE_COOLDOWN_SECONDS", "300")
                )


def target_from_connect(header: bytes) -> tuple[str, int]:
    try:
        line = header.split(b"\r\n", 1)[0].decode("ascii")
        method, authority, version = line.split(" ")
        if method != "CONNECT" or not version.startswith("HTTP/"):
            raise ValueError
        host, port = authority.rsplit(":", 1)
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        if not host or not 1 <= int(port) <= 65535:
            raise ValueError
        return host, int(port)
    except (UnicodeDecodeError, ValueError):
        raise ValueError("only CONNECT host:port is accepted") from None


async def connect_upstream(candidate: dict[str, Any], target: tuple[str, int], credentials: CredentialStore) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    endpoint, protocol = str(candidate["endpoint"]), str(candidate["protocol"])
    host = str(candidate.get("host") or "")
    port_value = candidate.get("port")
    if not host or port_value is None:
        host, port_text = endpoint.rsplit(":", 1)
        host, port_value = host.strip("[]"), int(port_text)
    auth = credentials.get(candidate.get("credential_ref"))
    ssl_context = ssl.create_default_context() if protocol == "https" else None
    reader, writer = await asyncio.open_connection(host, int(port_value), ssl=ssl_context, server_hostname=host if ssl_context else None)
    if protocol in {"http", "https"}:
        token = ""
        if auth:
            encoded = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
            token = f"Proxy-Authorization: Basic {encoded}\r\n"
        authority = f"{target[0]}:{target[1]}"
        writer.write(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n{token}Proxy-Connection: keep-alive\r\n\r\n".encode())
        await writer.drain()
        reply = await reader.readuntil(b"\r\n\r\n")
        if not reply.startswith(b"HTTP/") or b" 2" not in reply.split(b"\r\n", 1)[0]:
            raise OSError("upstream CONNECT rejected")
    elif protocol == "socks5":
        methods = b"\x00\x02" if auth else b"\x00"
        writer.write(b"\x05" + bytes([len(methods)]) + methods)
        await writer.drain()
        if await reader.readexactly(2) != (b"\x05\x02" if auth else b"\x05\x00"):
            raise OSError("SOCKS5 authentication negotiation failed")
        if auth:
            username, password = auth
            raw_user, raw_pass = username.encode(), password.encode()
            if len(raw_user) > 255 or len(raw_pass) > 255:
                raise ValueError("SOCKS5 credential is too long")
            writer.write(b"\x01" + bytes([len(raw_user)]) + raw_user + bytes([len(raw_pass)]) + raw_pass)
            await writer.drain()
            if await reader.readexactly(2) != b"\x01\x00":
                raise OSError("SOCKS5 authentication rejected")
        try:
            packed = socket.inet_pton(socket.AF_INET, target[0]); request = b"\x05\x01\x00\x01" + packed
        except OSError:
            raw_host = target[0].encode("idna")
            if len(raw_host) > 255:
                raise ValueError("target hostname is too long")
            request = b"\x05\x01\x00\x03" + bytes([len(raw_host)]) + raw_host
        writer.write(request + target[1].to_bytes(2, "big"))
        await writer.drain()
        response = await reader.readexactly(4)
        if response[:2] != b"\x05\x00":
            raise OSError("SOCKS5 CONNECT rejected")
        if response[3] == 1:
            address_size = 4
        elif response[3] == 3:
            address_size = (await reader.readexactly(1))[0]
        elif response[3] == 4:
            address_size = 16
        else:
            raise OSError("invalid SOCKS5 bind address")
        await reader.readexactly(address_size + 2)
    elif protocol == "socks4":
        username = auth[0].encode() if auth else b""
        if len(username) > 255:
            raise ValueError("SOCKS4 username is too long")
        try:
            address = socket.inet_aton(target[0])
            suffix = b""
        except OSError:
            address = b"\x00\x00\x00\x01"  # SOCKS4a hostname form
            suffix = target[0].encode("idna") + b"\x00"
        writer.write(b"\x04\x01" + target[1].to_bytes(2, "big") + address + username + b"\x00" + suffix)
        await writer.drain()
        response = await reader.readexactly(8)
        if response[1] != 0x5A:
            raise OSError("SOCKS4 CONNECT rejected")
    else:
        raise ValueError("unsupported upstream protocol")

    return reader, writer


async def probe_dns_tcp(candidate: dict[str, Any], target: tuple[str, int], credentials: CredentialStore) -> None:
    """Verify DNS-over-TCP in a disposable tunnel; do not consume the client tunnel."""
    reader, writer = await connect_upstream(candidate, target, credentials)
    try:
        query_id = os.urandom(2)
        query = query_id + b"\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00" + b"\x07example\x03com\x00\x00\x01\x00\x01"
        timeout = float(os.getenv("DNS_TCP_PROBE_TIMEOUT_SECONDS", "8"))
        writer.write(len(query).to_bytes(2, "big") + query)
        await asyncio.wait_for(writer.drain(), timeout)
        response_length = int.from_bytes(await asyncio.wait_for(reader.readexactly(2), timeout), "big")
        response = await asyncio.wait_for(reader.readexactly(response_length), timeout)
        if len(response) < 12 or response[:2] != query_id:
            raise OSError("upstream DNS-over-TCP probe failed")
    finally:
        writer.close()
        await writer.wait_closed()


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()


async def handle(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter, pool: Pool, credentials: CredentialStore) -> None:
    try:
        request = await asyncio.wait_for(client_reader.readuntil(b"\r\n\r\n"), 10)
        if len(request) > MAX_HEADERS:
            raise ValueError("CONNECT headers are too large")
        target = target_from_connect(request)
        attempts = int(os.getenv("UPSTREAM_CONNECT_ATTEMPTS", "8"))
        tried: set[str] = set()
        upstream_reader = upstream_writer = None

        for _ in range(attempts):
            candidate = await pool.choose(target[1], tried)
            if candidate is None:
                break
            tried.add(pool.key(candidate))
            try:
                if target[1] == 53:
                    await asyncio.wait_for(probe_dns_tcp(candidate, target, credentials), 15)
                upstream_reader, upstream_writer = await asyncio.wait_for(
                    connect_upstream(candidate, target, credentials), 15
                )
            except (asyncio.TimeoutError, OSError, ValueError) as error:
                print(f"[egress-relay] {candidate.get('endpoint')} -> {target} failed: {type(error).__name__}: {error}", flush=True)
                await pool.record_result(candidate, target[1], False)
                continue
            await pool.record_result(candidate, target[1], True)
            break

        if upstream_reader is None or upstream_writer is None:
            raise OSError("no upstream accepted the requested target")
        client_writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await client_writer.drain()
        await asyncio.gather(pipe(client_reader, upstream_writer), pipe(upstream_reader, client_writer))
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, OSError, ValueError, json.JSONDecodeError):
        client_writer.write(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
        await client_writer.drain()
        client_writer.close()


async def main() -> None:
    pool = Pool(os.environ["REDIS_URL"], int(os.getenv("POOL_SIZE", "150")))
    credentials = CredentialStore(os.getenv("PROXY_CREDENTIALS_FILE", "/run/secrets/proxy-credentials.json"))
    server = await asyncio.start_server(lambda r, w: handle(r, w, pool, credentials), "127.0.0.1", int(os.getenv("EGRESS_RELAY_PORT", "12000")), limit=MAX_HEADERS)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
