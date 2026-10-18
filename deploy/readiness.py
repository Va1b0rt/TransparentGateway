"""Local deployment gate. Full client/DNS/public readiness is a later stage."""
import json
import socket
import ssl
import http.client
import uuid
import time
from pool_health import assess
from urllib.parse import urlsplit

SERVICES = {'redis', 'fetcher', 'validator', 'ranker', 'asn-updater', 'gateway'}


def check(compose, run, values, since=None):
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
        if since is not None and data['generated_at'] < since:
            raise RuntimeError(kind+' snapshot predates replacement')
        if since is not None and kind != 'raw' and data.get('validator_cycle', {}).get('completed_at', 0) < since:
            raise RuntimeError(kind+' validator cycle predates replacement')
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
    exits = {entry.get('exit_ip') for entry in snapshots['active']['entries']}
    echo = relay_echo(values['ANONYMITY_CHECK_URL'])
    if echo.get('backend_peer') not in exits:
        raise RuntimeError('relay echo does not match validated active exit')
    if values.get('LAB_LOCAL_ECHO_ENABLED') == '1':
        local = relay_echo('http://172.30.40.10:8000/')
        if local.get('backend_peer') != '172.30.40.11' or local['backend_peer'] not in exits:
            raise RuntimeError('local fixture exit not active')
    return {'scope': 'host', 'services': len(rows), 'snapshot_age_seconds': ages,
            'pool_counts': counts, 'relay_echo': 'PASS', 'pipeline': pipeline,
            'active_exit_ips': sorted(exits), 'client_dns_public': 'pending'}


def relay_echo(url):
    target = urlsplit(url)
    port = target.port or (443 if target.scheme == 'https' else 80)
    stream = socket.create_connection(('127.0.0.1',12000),8)
    stream.settimeout(15)
    try:
        stream.sendall(f'CONNECT {target.hostname}:{port} HTTP/1.1\r\nHost: {target.hostname}:{port}\r\n\r\n'.encode())
        response = stream.makefile('rb')
        if b' 200 ' not in response.readline(4096):
            raise RuntimeError('relay CONNECT failed')
        size=0
        while True:
            line=response.readline(4096)
            size+=len(line)
            if not line or size>16384:raise RuntimeError('invalid CONNECT headers')
            if line==b'\r\n':break
        response.close()
        if target.scheme=='https':
            stream=ssl.create_default_context().wrap_socket(stream,server_hostname=target.hostname)
        marker=uuid.uuid4().hex
        stream.sendall((f'GET {target.path or "/"} HTTP/1.1\r\nHost: {target.hostname}:{port}\r\n'
            f'User-Agent: TG-Readiness/1.0\r\nX-Request-ID: {marker}\r\nConnection: close\r\n\r\n').encode())
        response=http.client.HTTPResponse(stream)
        response.begin()
        if response.status!=200:raise RuntimeError('echo HTTP status rejected')
        raw=response.read(65537)
        if len(raw)>65536:raise RuntimeError('oversize echo')
        echo=json.loads(raw)
        headers={k.lower():v.strip() for k,v in echo['headers'].items()}
        if headers.get('x-request-id')!=marker:raise RuntimeError('echo correlation failed')
        if any(k in headers for k in ('authorization','proxy-authorization','cookie')):
            raise RuntimeError('unexpected credential headers')
        return echo
    finally:
        stream.close()
