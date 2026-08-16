import asyncio
import importlib.util
import json
from pathlib import Path

MODULE = Path(__file__).parents[1] / "gateway" / "egress_relay.py"
spec = importlib.util.spec_from_file_location("egress_relay", MODULE)
relay = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(relay)


def test_connect_target_rejects_non_connect_requests():
    assert relay.target_from_connect(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n") == ("example.com", 443)
    try:
        relay.target_from_connect(b"GET / HTTP/1.1\r\n\r\n")
    except ValueError:
        pass
    else:
        raise AssertionError("non-CONNECT request accepted")


def test_relay_adds_upstream_basic_auth_from_secret_ref(tmp_path):
    secret_file = tmp_path / "credentials.json"
    secret_file.write_text(json.dumps({"provider-a": {"username": "user", "password": "pass"}}))

    async def run():
        seen = {}

        async def upstream(reader, writer):
            request = await reader.readuntil(b"\r\n\r\n")
            seen["request"] = request
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            payload = await reader.readexactly(4)
            writer.write(payload)
            await writer.drain()
            writer.close()

        upstream_server = await asyncio.start_server(upstream, "127.0.0.1", 0)
        upstream_port = upstream_server.sockets[0].getsockname()[1]

        class FixedPool:
            @staticmethod
            def key(candidate):
                return f"{candidate['protocol']}://{candidate['endpoint']}"

            async def choose(self, target_port, excluded):
                return {"endpoint": f"127.0.0.1:{upstream_port}", "protocol": "http", "credential_ref": "provider-a"}

            async def record_result(self, candidate, target_port, success):
                pass

        gateway_server = await asyncio.start_server(
            lambda reader, writer: relay.handle(reader, writer, FixedPool(), relay.Credentials(str(secret_file))),
            "127.0.0.1", 0,
        )
        gateway_port = gateway_server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", gateway_port)
        writer.write(b"CONNECT target.example:443 HTTP/1.1\r\n\r\n")
        await writer.drain()
        assert await reader.readuntil(b"\r\n\r\n") == b"HTTP/1.1 200 Connection established\r\n\r\n"
        writer.write(b"ping")
        await writer.drain()
        assert await reader.readexactly(4) == b"ping"
        writer.close()
        await writer.wait_closed()
        gateway_server.close()
        upstream_server.close()
        await gateway_server.wait_closed()
        await upstream_server.wait_closed()
        return seen["request"]

    request = asyncio.run(run())
    assert b"Proxy-Authorization: Basic dXNlcjpwYXNz" in request
