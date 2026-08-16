"""Transparent UDP DNS-to-TCP relay for authorised gateway clients."""
from __future__ import annotations

import asyncio
import os
import socket
import struct

IP_TRANSPARENT = 19
IP_RECVORIGDSTADDR = 20
IP_ORIGDSTADDR = 20
IP_PKTINFO = 8
SO_MARK = 36
DNS_SOCKET_MARK = 0x53


def transparent_socket(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_IP, IP_TRANSPARENT, 1)
    sock.setsockopt(socket.SOL_IP, IP_RECVORIGDSTADDR, 1)
    sock.setsockopt(socket.SOL_IP, IP_PKTINFO, 1)
    sock.bind(("0.0.0.0", port))
    sock.setblocking(False)
    return sock


def original_destination(ancillary: list[tuple[int, int, bytes]]) -> tuple[str, int]:
    for level, kind, data in ancillary:
        if level == socket.SOL_IP and kind == IP_ORIGDSTADDR and len(data) >= 8:
            family = struct.unpack("=H", data[:2])[0]
            if family == socket.AF_INET:
                return socket.inet_ntoa(data[4:8]), struct.unpack("!H", data[2:4])[0]
    # TPROXY delivers the original destination address through IP_PKTINFO on
    # some Linux kernels. DNS interception is limited to UDP port 53.
    for level, kind, data in ancillary:
        if level == socket.SOL_IP and kind == IP_PKTINFO and len(data) >= 12:
            return socket.inet_ntoa(data[8:12]), 53
    raise ValueError("TPROXY packet is missing original destination metadata")


async def tcp_exchange(payload: bytes, destination: tuple[str, int], timeout: float) -> bytes:
    if len(payload) > 65535:
        raise ValueError("DNS payload exceeds TCP framing limit")
    outbound = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    outbound.setsockopt(socket.SOL_SOCKET, SO_MARK, DNS_SOCKET_MARK)
    outbound.setblocking(False)
    writer = None
    try:
        loop = asyncio.get_running_loop()
        await asyncio.wait_for(loop.sock_connect(outbound, destination), timeout)
        reader, writer = await asyncio.open_connection(sock=outbound)
        writer.write(struct.pack("!H", len(payload)) + payload)
        await asyncio.wait_for(writer.drain(), timeout)
        length = struct.unpack("!H", await asyncio.wait_for(reader.readexactly(2), timeout))[0]
        return await asyncio.wait_for(reader.readexactly(length), timeout)
    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()
        else:
            outbound.close()


def reply(payload: bytes, client: tuple[str, int], source: tuple[str, int]) -> None:
    """Return a UDP response with the original DNS server as its source."""
    source_ip, source_port = source
    reply_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        reply_sock.setsockopt(socket.SOL_IP, IP_TRANSPARENT, 1)
        reply_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        reply_sock.bind((source_ip, source_port))
        reply_sock.sendto(payload, client)
    finally:
        reply_sock.close()


async def serve() -> None:
    sock = transparent_socket(int(os.getenv("DNS_TPROXY_PORT", "5353")))
    timeout = float(os.getenv("DNS_TCP_TIMEOUT_SECONDS", "8"))
    limit = asyncio.Semaphore(int(os.getenv("DNS_MAX_CONCURRENCY", "256")))
    loop = asyncio.get_running_loop()

    async def recvmsg() -> tuple[bytes, list[tuple[int, int, bytes]], int, tuple[str, int]]:
        """Wait for socket readiness before calling recvmsg on a nonblocking FD."""
        ready = loop.create_future()
        loop.add_reader(sock.fileno(), ready.set_result, None)
        try:
            await ready
            return sock.recvmsg(65535, socket.CMSG_SPACE(16))
        finally:
            loop.remove_reader(sock.fileno())

    async def handle(data: bytes, client: tuple[str, int], destination: tuple[str, int]) -> None:
        async with limit:
            try:
                response = await tcp_exchange(data, destination, timeout)
                print(f"[udp2tcp-dns] tcp response {len(response)} bytes from {destination}", flush=True)
                reply(response, client, destination)
                print(f"[udp2tcp-dns] udp reply sent to {client}", flush=True)
            except (asyncio.TimeoutError, OSError, ValueError, asyncio.IncompleteReadError) as error:
                print(f"[udp2tcp-dns] {client} -> {destination} failed: {type(error).__name__}: {error}", flush=True)

    try:
        while True:
            data, ancillary, _, client = await recvmsg()
            try:
                destination = original_destination(ancillary)
            except ValueError as error:
                print(f"[udp2tcp-dns] dropped malformed TPROXY packet: {error}", flush=True)
                continue
            asyncio.create_task(handle(data, client, destination))
    finally:
        sock.close()


if __name__ == "__main__":
    asyncio.run(serve())
