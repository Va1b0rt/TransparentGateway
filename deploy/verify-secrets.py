#!/usr/bin/python3
"""Lab evidence: permissions, consumer isolation, auth and bounded secret-leak audit."""
import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

from deploy import config_files, SAFE_ENV, validate, read_env
from readiness import check

ROOT = Path('/opt/transparent-gateway')
CONFIG = Path('/etc/transparent-gateway/runtime')


def capture(command, timeout=90):
    result = subprocess.run(command, capture_output=True, env=SAFE_ENV, timeout=timeout)
    if result.returncode:
        raise RuntimeError('audit command failed')
    return result.stdout + result.stderr


def proxy_status(credential=None):
    auth = b''
    if credential:
        auth = b'Proxy-Authorization: Basic '+base64.b64encode((credential['username']+':'+credential['password']).encode())+b'\r\n'
    with socket.create_connection(('172.30.40.11',3128),5) as conn:
        conn.settimeout(5)
        conn.sendall(b'GET http://172.30.40.10:8000/ HTTP/1.1\r\nHost: 172.30.40.10:8000\r\n'+auth+b'Connection: close\r\n\r\n')
        return conn.recv(4096).split(b'\r\n',1)[0].split()[1]


def main():
    if os.geteuid() != 0:
        raise ValueError('root required')
    host = json.loads(Path('/etc/transparent-gateway/host.json').read_text())
    values = validate(CONFIG, host)
    validate(ROOT, host)
    for name in config_files(CONFIG):
        assert (CONFIG/name).read_bytes() == (ROOT/name).read_bytes()
    assert CONFIG.stat().st_mode & 0o777 == 0o700
    for name in ['secrets','provider-secrets']:
        assert (ROOT/name).stat().st_mode & 0o777 == 0o750
        assert (ROOT/name).stat().st_uid == 0 and (ROOT/name).stat().st_gid == 65532
    print('PASS permissions: canonical 0700; .env root:root 0600; secrets root:65532 0640', flush=True)
    credentials = json.loads((CONFIG/'secrets/proxy-credentials.json').read_text())
    current = credentials['lab-proxy']
    known = [entry[field] for entry in credentials.values() for field in ('username','password')]
    known.append((CONFIG/'provider-secrets/api-token').read_text().strip())
    previous = Path('/var/lib/transparent-gateway/previous-test-secrets.json')
    old = json.loads(previous.read_text()) if previous.exists() else None
    if old:
        known += [old['username'],old['password'],old['provider_token']]
    patterns = {v.encode() for v in known if v}
    patterns |= {base64.b64encode(v) for v in patterns}
    for entry in credentials.values():
        patterns.add(base64.b64encode((entry['username']+':'+entry['password']).encode()))
    if old:
        patterns.add(base64.b64encode((old['username']+':'+old['password']).encode()))
    def clean(data):
        if any(value in data for value in patterns):
            raise RuntimeError('secret material detected; values suppressed')
    ids = capture(['docker','ps','-aq','--filter','label=com.docker.compose.project=transparent-gateway']).decode().split()
    assert len(ids)==6
    inspect = capture(['docker','inspect',*ids])
    clean(inspect)
    for item in json.loads(inspect):
        service = item['Config']['Labels']['com.docker.compose.service']
        secret_mounts = [m for m in item['Mounts'] if 'secrets' in m['Destination']]
        expected = {'fetcher': [('provider-secrets','/run/provider-secrets')],
                    'validator':[('secrets','/run/secrets')], 'gateway':[('secrets','/run/secrets')]}.get(service,[])
        assert sorted((m['Source'],m['Destination']) for m in secret_mounts) == sorted((str(ROOT/a),b) for a,b in expected)
        assert all(not m['RW'] for m in secret_mounts)
        for source, destination in expected:
            filename = 'api-token' if source=='provider-secrets' else 'proxy-credentials.json'
            code = 'import hashlib,pathlib; print(hashlib.sha256(pathlib.Path('+repr(destination+'/'+filename)+').read_bytes()).hexdigest())'
            digest = capture(['docker','exec','--user','65532:65532',item['Id'],'python','-c',code]).decode().strip()
            assert digest == hashlib.sha256((ROOT/source/filename).read_bytes()).hexdigest()
        clean(capture(['docker','logs',item['Id']]))
    fixture_ids = capture(['docker','ps','-aq','--filter','label=com.docker.compose.project=tg-lab']).decode().split()
    for identifier in fixture_ids:
        clean(capture(['docker','inspect',identifier]))
        clean(capture(['docker','logs',identifier]))
    print('PASS read-only mounts: provider token -> fetcher; proxy credentials -> validator/gateway', flush=True)
    assert proxy_status() == b'407'
    assert proxy_status({'username':'wrong','password':'wrong'}) == b'407'
    assert proxy_status(current) == b'200'
    if old:
        assert proxy_status(old) == b'407'
    print('PASS proxy auth: absent/wrong'+('/previous' if old else '')+' credentials=407; current=200', flush=True)
    compose = ['docker','compose','--project-name','transparent-gateway','--project-directory',str(ROOT),
               '--env-file',str(ROOT/'.env'),'-f',str(ROOT/'docker-compose.yml'),'-f',str(ROOT/'deploy/lab/gateway.override.yml')]
    result = check(compose, lambda cmd, **kwargs: capture(cmd).decode().strip(), values)
    print('PASS fresh pools and authenticated local relay echo', flush=True)
    clean(capture(['journalctl','--no-pager','--output=cat']))
    for folder in [Path('/var/lib/cloud'),Path('/var/log')]:
        for path in folder.rglob('*'):
            if path.is_file() and not path.is_symlink() and (folder.name=='cloud' or 'cloud-init' in path.name):
                clean(path.read_bytes())
    for repository in [ROOT,Path('/var/lib/transparent-gateway/source')]:
        for name in capture(['git','-C',str(repository),'ls-files']).decode().splitlines():
            clean((repository/name).read_bytes())
    redis = ['docker','exec','transparent-gateway-redis-1','redis-cli','--raw']
    for key in capture(redis+['KEYS','*']).decode().splitlines():
        if capture(redis+['TYPE',key]).strip()==b'string':
            clean(capture(redis+['GET',key]))
    print('PASS known test secrets absent: tracked files, cloud-init, journal, container logs/inspect, Redis strings', flush=True)
    print('SECRETS_CHECK PASS — all configured proxy credentials scanned; real provider API not exercised', flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('SECRETS_CHECK FAILED: '+type(error).__name__+'; details suppressed', flush=True)
        raise SystemExit(1)
