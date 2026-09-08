"""Bounded local fixture: JSON echo and an HTTP proxy restricted to that echo."""
import asyncio
import base64
import hmac
from pathlib import Path
import json
import sys
from urllib.parse import urlsplit

ECHO_HOST, ECHO_PORT = '172.30.40.10', 8000


async def response(writer, status, body=b''):
    writer.write(f'HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n'.encode()+body)
    await writer.drain()


async def handle(reader, writer):
    upstream = None
    try:
        raw = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 10)
        lines = raw.decode('iso-8859-1').split('\r\n')
        method, target, _ = lines[0].split(' ', 2)
        if sys.argv[1] == 'echo':
            if method != 'GET':
                await response(writer, '405 Method Not Allowed')
                return
            headers = dict(line.split(':', 1) for line in lines[1:] if ':' in line)
            body = json.dumps({'backend_peer': writer.get_extra_info('peername')[0], 'headers': headers}).encode()
            await response(writer, '200 OK', body)
            return
        credentials = json.loads(Path('/run/secrets/proxy-credentials.json').read_text())
        if credentials:
            entry = credentials['lab-proxy']
            expected = 'Basic '+base64.b64encode((entry['username']+':'+entry['password']).encode()).decode()
            headers = {k.lower(): v.strip() for k,v in (line.split(':',1) for line in lines[1:] if ':' in line)}
            if not hmac.compare_digest(headers.get('proxy-authorization',''), expected):
                await response(writer, '407 Proxy Authentication Required')
                return
        # This fixture is deliberately unable to proxy arbitrary destinations.
        if method == 'CONNECT':
            if target != f'{ECHO_HOST}:{ECHO_PORT}':
                await response(writer, '403 Forbidden')
                return
        elif method == 'GET':
            url = urlsplit(target)
            if url.scheme != 'http' or url.hostname != ECHO_HOST or url.port != ECHO_PORT:
                await response(writer, '403 Forbidden')
                return
        else:
            await response(writer, '405 Method Not Allowed')
            return
        remote, upstream = await asyncio.wait_for(asyncio.open_connection(ECHO_HOST, ECHO_PORT), 5)
        if method == 'CONNECT':
            writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            await writer.drain()
        else:
            # No forwarding identity headers are injected by the proxy.
            upstream.write(f'GET {url.path or "/"} HTTP/1.1\r\nHost: {ECHO_HOST}:{ECHO_PORT}\r\nConnection: close\r\n\r\n'.encode())
            await upstream.drain()

        async def copy(source, destination):
            while data := await source.read(65536):
                destination.write(data)
                await destination.drain()
        if method == 'GET':
            await asyncio.wait_for(copy(remote, writer), 15)
        else:
            tasks = [asyncio.create_task(copy(reader, upstream)), asyncio.create_task(copy(remote, writer))]
            try:
                await asyncio.wait(tasks, timeout=20, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    except (OSError, ValueError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        pass
    finally:
        writer.close()
        if upstream:
            upstream.close()


async def main():
    server = await asyncio.start_server(handle, '0.0.0.0', 8000 if sys.argv[1] == 'echo' else 3128, limit=16384)
    async with server:
        await server.serve_forever()


asyncio.run(main())
