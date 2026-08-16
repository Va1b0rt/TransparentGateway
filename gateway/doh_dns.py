"""Minimal UDP DNS to Cloudflare DoH relay with fixed IP and TLS SNI.

The fixed endpoint prevents bootstrap DNS traffic from escaping before the DoH
relay is available. TLS validation still uses the configured server name.
"""
from __future__ import annotations

import asyncio
import http.client
import os
import socket
import ssl

SO_MARK = 36
DNS_SOCKET_MARK = 0x53


def doh_request(message: bytes, timeout: float) -> bytes:
    endpoint_ip = os.getenv("DNS_DOH_IP", "1.1.1.1")
    server_name = os.getenv("DNS_DOH_SERVER_NAME", "cloudflare-dns.com")
    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    raw.setsockopt(socket.SOL_SOCKET, SO_MARK, DNS_SOCKET_MARK)
    raw.settimeout(timeout)
    raw.connect((endpoint_ip, 443))
    tls = ssl.create_default_context().wrap_socket(raw, server_hostname=server_name)
    try:
        request = (
            f"POST /dns-query HTTP/1.1\r\nHost: {server_name}\r\n"
            "Content-Type: application/dns-message\r\nAccept: application/dns-message\r\n"
            f"Content-Length: {len(message)}\r\nConnection: close\r\n\r\n"
        ).encode() + message
        tls.sendall(request)
        response = http.client.HTTPResponse(tls)
        response.begin()
        body = response.read()
        if response.status != 200 or not body:
            raise OSError(f"DoH HTTP status {response.status}")
        return body
    finally:
        tls.close()


class DohProtocol(asyncio.DatagramProtocol):
    def __init__(self, timeout: float, concurrency: int) -> None:
        self.timeout = timeout
        self.limit = asyncio.Semaphore(concurrency)
        self.transport: asyncio.DatagramTransport

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, address: tuple[str, int]) -> None:
        asyncio.create_task(self.forward(data, address))

    async def forward(self, data: bytes, address: tuple[str, int]) -> None:
        async with self.limit:
            try:
                reply = await asyncio.to_thread(doh_request, data, self.timeout)
                self.transport.sendto(reply, address)
            except OSError as error:
                print(f"[doh-dns] {address} failed: {type(error).__name__}", flush=True)


async def main() -> None:
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(
        lambda: DohProtocol(float(os.getenv("DNS_DOH_TIMEOUT_SECONDS", "8")), int(os.getenv("DNS_MAX_CONCURRENCY", "256"))),
        local_addr=("0.0.0.0", int(os.getenv("DNS_DOH_PORT", "5053"))),
    )
    await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
