"""Local deployment gate. Full client/DNS/public readiness is a later stage."""
import json
import socket
import time
from pool_health import assess
from urllib.parse import urlsplit

SERVICES = {'redis', 'fetcher', 'validator', 'ranker', 'asn-updater', 'gateway'}


def check(compose, run, values):
    # Query only status/IDs: never print docker inspect environment values.
    rows = [json.loads(line) for line in run(compose+['ps', '--all', '--format', 'json']).splitlines() if line]
    if len(rows) == 1 and isinstance(rows[0], list):
        rows = rows[0]
    if {row['Service'] for row in rows} != SERVICES:
        raise RuntimeError('not all six services exist')
    for row in rows:
        if row['State'] != 'running' or row.get('Health', '') not in ('', 'healthy'):
            raise RuntimeError('service not running/healthy: '+row['Service'])
    ages = {}
    counts = {}
    snapshots = {}
    limits = {'raw': max(60, 3*int(values['FETCH_INTERVAL_SECONDS'])),
              'validated': max(60, 3*int(values['VALIDATION_INTERVAL_SECONDS'])),
              'reserve': max(60, 3*int(values['RANK_INTERVAL_SECONDS'])),
              'active': max(60, 3*int(values['RANK_INTERVAL_SECONDS']))}
    for kind in limits:
        data = json.loads(run(compose+['exec', '-T', 'redis', 'redis-cli', '--raw', 'GET', f'proxy_pool:{kind}:v1']))
        if not isinstance(data, dict) or not isinstance(data.get('entries'), list) or not data['entries']:
            raise RuntimeError(kind+' pool empty or invalid')
        age = time.time()-data['generated_at']
        if not -5 <= age <= limits[kind]:
            raise RuntimeError(kind+' snapshot stale')
        ages[kind], counts[kind], snapshots[kind] = round(age), len(data['entries']), data
    last_success = json.loads(run(compose+['exec','-T','redis','redis-cli','--raw','GET','validator:last_success:v1']) or 'null')
    pipeline = assess(snapshots,last_success,time.time(),limits)
    # A recently re-ranked stale validator snapshot must not count as ready.
    source_age = time.time()-snapshots['validated']['source_generated_at']
    if not -5 <= source_age <= limits['raw']+limits['validated']:
        raise RuntimeError('validated source snapshot stale')
    compatibility = json.loads(run(compose+['exec', '-T', 'redis', 'redis-cli', '--raw', 'GET', 'transparent-gateway:validated:v1']))
    # Publications can straddle this read. Require equal active membership, not timestamp.
    if [e['address'] for e in compatibility['entries']] != [e['address'] for e in snapshots['active']['entries']]:
        raise RuntimeError('gateway active membership differs')
    target = urlsplit(values['ANONYMITY_CHECK_URL'])
    # Local lab smoke test only. Public HTTPS/client readiness is deliberately separate.
    if target.scheme != 'http':
        raise RuntimeError('stage-3 local gate requires an HTTP echo; full HTTPS gate not implemented yet')
    port = target.port or 80
    with socket.create_connection(('127.0.0.1', 12000), 5) as stream:
        stream.settimeout(8)
        stream.sendall(f'CONNECT {target.hostname}:{port} HTTP/1.1\r\nHost: {target.hostname}:{port}\r\n\r\n'.encode())
        response = stream.makefile('rb')
        status = response.readline(4096)
        if b' 200 ' not in status:
            raise RuntimeError('relay CONNECT failed')
        while True:
            line = response.readline(4096)
            if line == b'\r\n':
                break
            if not line:
                raise RuntimeError('incomplete relay response')
        stream.sendall(f'GET {target.path or "/"} HTTP/1.1\r\nHost: {target.hostname}:{port}\r\nConnection: close\r\n\r\n'.encode())
        if b' 200 ' not in response.readline(4096):
            raise RuntimeError('echo through relay failed')
        headers = {}
        while True:
            line = response.readline(4096)
            if line == b'\r\n':
                break
            if not line:
                raise RuntimeError('incomplete echo headers')
            name, value = line.decode().split(':', 1)
            headers[name.lower()] = value.strip()
        length = int(headers.get('content-length', 0))
        if not 0 < length <= 65536:
            raise RuntimeError('echo body invalid')
        echo = json.loads(response.read(length))
        exits = {entry.get('exit_ip') for entry in snapshots['active']['entries']}
        if echo.get('backend_peer') not in exits or not isinstance(echo.get('headers'), dict):
            raise RuntimeError('relay echo does not match validated exit')
    return {'scope': 'local', 'services': len(rows), 'snapshot_age_seconds': ages,
            'pool_counts': counts, 'relay_echo': 'PASS', 'pipeline': pipeline,
            'active_exit_ips': sorted(exits), 'client_dns_public': 'pending'}
