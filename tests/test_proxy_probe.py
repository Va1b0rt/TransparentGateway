import asyncio
import json
import socket
import sys
from pathlib import Path

VALIDATOR_DIR = str(Path(__file__).parents[1] / "validator")
sys.path.insert(0, VALIDATOR_DIR)

from proxy_probe import CheckTarget, EchoObservation, classify_anonymity, proxy_observation
from sources import ProxyCandidate


def test_anonymity_classification_rejects_peer_or_forwarded_ip_leak():
    direct = {"192.0.2.10"}
    assert classify_anonymity(EchoObservation("192.0.2.10", {}), direct) is None
    assert classify_anonymity(EchoObservation(
        "198.51.100.8", {"x-forwarded-for": "192.0.2.10"}
    ), direct) is None
    assert classify_anonymity(EchoObservation(
        "198.51.100.8", {"via": "1.1 test-proxy"}
    ), direct) == "anonymous"
    assert classify_anonymity(EchoObservation("198.51.100.8", {}), direct) == "elite"


async def _socks5_probe_scenario():
    async def echo(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        body = json.dumps({"backend_peer": "198.51.100.8", "headers": {}}).encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body
        )
        await writer.drain()
        writer.close()

    echo_server = await asyncio.start_server(echo, "127.0.0.1", 0)
    echo_port = echo_server.sockets[0].getsockname()[1]

    async def relay(source, destination):
        try:
            while data := await source.read(65536):
                destination.write(data)
                await destination.drain()
        finally:
            destination.close()

    async def socks5(reader, writer):
        try:
            assert await reader.readexactly(3) == b"\x05\x01\x00"
            writer.write(b"\x05\x00")
            await writer.drain()
            version, command, reserved, address_type = await reader.readexactly(4)
            assert (version, command, reserved) == (5, 1, 0)
            if address_type == 1:
                host = socket.inet_ntoa(await reader.readexactly(4))
            elif address_type == 3:
                host = (await reader.readexactly((await reader.readexactly(1))[0])).decode()
            else:
                raise AssertionError("unexpected target address type")
            port = int.from_bytes(await reader.readexactly(2), "big")
            upstream_reader, upstream_writer = await asyncio.open_connection(host, port)
            writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            await writer.drain()
            await asyncio.gather(relay(reader, upstream_writer), relay(upstream_reader, writer))
        finally:
            writer.close()

    socks_server = await asyncio.start_server(socks5, "127.0.0.1", 0)
    socks_port = socks_server.sockets[0].getsockname()[1]
    candidate = ProxyCandidate("127.0.0.1", socks_port, "socks5", "test")
    target = CheckTarget.parse(f"http://127.0.0.1:{echo_port}/echo")
    try:
        observation, latency_ms = await proxy_observation(candidate, target, 2)
        assert observation.peer == "198.51.100.8"
        assert latency_ms >= 1
    finally:
        socks_server.close()
        echo_server.close()
        await socks_server.wait_closed()
        await echo_server.wait_closed()


def test_socks5_proxy_performs_real_connect_and_echo_request():
    asyncio.run(_socks5_probe_scenario())
