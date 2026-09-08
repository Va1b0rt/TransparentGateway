#!/usr/bin/python3
"""Deploy a full commit, reconcile runtime files, and wait for local readiness."""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time
from urllib.parse import urlsplit

from readiness import check as readiness_check

SAFE_ENV = {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
            'LANG': 'C.UTF-8', 'COMPOSE_PARALLEL_LIMIT': '1'}
STATE = Path('/var/lib/transparent-gateway/deployment.json')
FILES = ['.env', 'inventory/connectors.json', 'inventory/upstreams.jsonl', 'secrets/proxy-credentials.json']


def run(command, timeout=120):
    result = subprocess.run([str(c) for c in command], capture_output=True, text=True,
                            env=SAFE_ENV, timeout=timeout)
    if result.returncode:
        # stderr may contain expanded config. Do not send it to journal or console.
        raise RuntimeError(f'{Path(command[0]).name} command failed (exit {result.returncode})')
    return result.stdout.strip()


def read_env(path):
    values = {}
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        if '=' not in line:
            raise ValueError(f'invalid .env line {number}')
        key, value = line.split('=', 1)
        if not re.fullmatch(r'[A-Z][A-Z0-9_]*', key) or key in values:
            raise ValueError(f'invalid or duplicate .env key on line {number}')
        if any(char in value for char in ['$', '\\', '"', "'", '`', '\r']):
            raise ValueError(f'.env requires literal unquoted values on line {number}')
        values[key] = value
    return values


def validate(config, host):
    for name in FILES:
        path = config/name
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0:
            raise ValueError('config files must be regular and root-owned: '+name)
        expected = 0o600 if name == '.env' else 0o640
        if stat.S_IMODE(info.st_mode) != expected:
            raise ValueError('incorrect config permissions: '+name)
        if name != '.env' and info.st_gid != 65532:
            raise ValueError('container-readable config requires GID 65532: '+name)
    values = read_env(config/'.env')
    client = ipaddress.IPv4Network(values['CLIENT_CIDR'], strict=True)
    client_interface = ipaddress.IPv4Interface(host['interfaces']['eth1'])
    if client != client_interface.network or client.overlaps(ipaddress.IPv4Interface(host['interfaces']['eth0']).network):
        raise ValueError('CLIENT_CIDR must match eth1 and exclude management')
    if values['VALIDATOR_REDIS_URL'] != 'redis://redis:6379/0' or values['GATEWAY_REDIS_URL'] != 'redis://127.0.0.1:6379/0':
        raise ValueError('this deployment requires the included local Redis service')
    for key in ['POOL_SIZE','RESERVE_SIZE','VALIDATION_CONCURRENCY','VALIDATION_INTERVAL_SECONDS',
                'FETCH_INTERVAL_SECONDS','RANK_INTERVAL_SECONDS','DNS_MAX_CONCURRENCY','DNS_TCP_TIMEOUT_SECONDS']:
        if int(values[key]) <= 0:
            raise ValueError('expected positive config integer: '+key)
    if int(values['POOL_SIZE']) > int(values['RESERVE_SIZE']):
        raise ValueError('POOL_SIZE exceeds RESERVE_SIZE')
    for address in values['DNS_DEFAULT_RESOLVER_IPS'].split(',')+[values['DNS_DOH_IP']]:
        ipaddress.IPv4Address(address.strip())
    echo = urlsplit(values['ANONYMITY_CHECK_URL'])
    if echo.scheme != 'http' or not echo.hostname or echo.username or echo.password or echo.fragment:
        raise ValueError('stage-3 local readiness requires an HTTP echo without URL credentials')
    credentials = json.loads((config/'secrets/proxy-credentials.json').read_text())
    if not isinstance(credentials, dict) or any(not isinstance(v, dict) or
            not isinstance(v.get('username'), str) or not isinstance(v.get('password'), str) for v in credentials.values()):
        raise ValueError('invalid proxy credential store')
    connectors = json.loads((config/'inventory/connectors.json').read_text())
    allowed, active = connectors.get('allowed_connector_types'), connectors.get('active_connectors')
    if not isinstance(allowed, list) or not isinstance(active, list) or not active:
        raise ValueError('explicit allowed and active connectors are required')
    names = set()
    for connector in active:
        if connector.get('type') not in allowed or connector.get('type') not in ['jsonl_inventory','http_json_feed']:
            raise ValueError('connector is not explicitly allowed or registered')
        name = connector.get('name')
        if not name or name in names:
            raise ValueError('connector names must be nonempty and unique')
        names.add(name)
        if connector['type'] == 'jsonl_inventory' and connector.get('path') != '/inventory/upstreams.jsonl':
            raise ValueError('inventory connector must use the mounted upstream file')
    candidates = 0
    for line in (config/'inventory/upstreams.jsonl').read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        candidate = json.loads(line)
        if not candidate.get('host') or not isinstance(candidate.get('port'), int) or not 1 <= candidate['port'] <= 65535:
            raise ValueError('inventory requires canonical host and port')
        if candidate.get('protocol') not in ['http','https','socks4','socks5']:
            raise ValueError('unsupported inventory protocol')
        if candidate.get('credential_ref') and candidate['credential_ref'] not in credentials:
            raise ValueError('inventory credential reference is missing')
        candidates += 1
    if any(c['type'] == 'jsonl_inventory' for c in active) and not candidates:
        raise ValueError('active inventory is empty')
    return values


def atomic_write(path, content, mode, gid=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError('refusing to replace a symlink: '+path.name)
    if path.exists() and path.read_bytes() == content and stat.S_IMODE(path.stat().st_mode) == mode and path.stat().st_uid == 0 and path.stat().st_gid == gid:
        return False
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(mode)
    os.chown(temporary, 0, gid)
    os.replace(temporary, path)
    return True


def deploy(args):
    if os.geteuid() != 0:
        raise ValueError('run with sudo')
    if not re.fullmatch('[0-9a-f]{40}', args.commit):
        raise ValueError('supply a full immutable 40-character commit SHA')
    target, config = Path(args.destination), Path(args.config_dir)
    if not target.is_absolute() or len(target.parts) < 3 or target.is_symlink():
        raise ValueError('destination must be an absolute application directory')
    if not config.is_absolute() or config.is_symlink() or config.is_relative_to(target):
        raise ValueError('canonical config must live outside checkout')
    source_url = urlsplit(args.source)
    if source_url.username or source_url.password or (source_url.scheme and source_url.scheme != 'https'):
        raise ValueError('use an HTTPS source without URL credentials, or an absolute local Git repository')
    if not source_url.scheme and not Path(args.source).is_absolute():
        raise ValueError('local Git source must be an absolute path')
    os.umask(0o077)
    STATE.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    lock = (STATE.parent/'deployment.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    started = time.monotonic()
    release = Path('/etc/os-release').read_text()
    if not re.search(r'^ID=debian$', release, re.M) or not re.search(r'^VERSION_ID="?13"?$', release, re.M):
        raise ValueError('Debian 13 required')
    host = json.loads(Path('/etc/transparent-gateway/host.json').read_text())
    addresses = {row['ifname']: row for row in json.loads(run(['ip','-j','-4','address']))}
    for name, cidr in host['interfaces'].items():
        row = addresses.get(name, {})
        if row.get('operstate') != 'UP' or cidr not in {f"{a['local']}/{a['prefixlen']}" for a in row.get('addr_info', [])}:
            raise ValueError('host network differs from declared NIC mapping: '+name)
    defaults = json.loads(run(['ip','-j','-4','route','show','default']))
    if len(defaults) != 1 or defaults[0].get('dev') != 'eth0' or defaults[0].get('gateway') != host['default_gateway']:
        raise ValueError('unexpected management default route')
    run(['docker','info','--format','{{.ServerVersion}}'])
    run(['docker','compose','version'])
    values = validate(config, host)
    print('PASS Debian, Docker, directories, NICs, config, inventory and credential references', flush=True)
    if target.exists() and not (target/'.git').is_dir() and any(target.iterdir()):
        raise ValueError('destination is nonempty and not a Git checkout')
    target.mkdir(parents=True, exist_ok=True)
    if stat.S_IMODE(target.stat().st_mode) != 0o755:
        target.chmod(0o755)
    if not (target/'.git').exists():
        run(['git','init',str(target)])
    git = ['git','-C',str(target)]
    if run(git+['status','--porcelain','--untracked-files=no']):
        raise ValueError('checkout has tracked local changes')
    run(git+['fetch','--no-tags',args.source,args.commit])
    if run(git+['rev-parse','FETCH_HEAD']) != args.commit:
        raise ValueError('fetched commit differs from requested SHA')
    run(git+['checkout','--detach',args.commit])
    # Git respects umask when creating code. Containers must read non-secret files.
    for name in run(git+['ls-files']).splitlines():
        file = target/name
        if file.is_file() and not file.is_symlink():
            mode = 0o755 if file.stat().st_mode & 0o111 else 0o644
            if stat.S_IMODE(file.stat().st_mode) != mode:
                file.chmod(mode)
        parent = file.parent
        while parent != target:
            mode = 0o750 if parent in [target/'inventory', target/'secrets'] else 0o755
            if stat.S_IMODE(parent.stat().st_mode) != mode:
                parent.chmod(mode)
            parent = parent.parent
    changed = 0
    for name in FILES:
        path = target/name
        path.parent.mkdir(exist_ok=True)
        if name != '.env':
            if (path.parent.stat().st_uid,path.parent.stat().st_gid) != (0,65532):
                os.chown(path.parent, 0, 65532)
            if stat.S_IMODE(path.parent.stat().st_mode) != 0o750:
                path.parent.chmod(0o750)
        changed += atomic_write(path, (config/name).read_bytes(), 0o600 if name == '.env' else 0o640,
                                0 if name == '.env' else 65532)
    compose = ['docker','compose','--project-name','transparent-gateway','--project-directory',str(target),
               '--env-file',str(target/'.env'),'-f',str(target/'docker-compose.yml')]
    if args.lab_fixture:
        compose += ['-f',str(target/'deploy/lab/gateway.override.yml')]
    run(compose+['config','--quiet'])
    print(f'COMMIT {args.commit}\nPASS docker compose config --quiet\nCONFIG_FILES_CHANGED {changed}', flush=True)
    before = set(run(compose+['ps','--all','--quiet']).splitlines())
    if args.lab_fixture:
        fixture = ['docker','compose','--project-name','tg-lab','-f',str(target/'deploy/lab/compose.yml')]
        run(fixture+['config','--quiet'])
        run(fixture+['up','--detach','--wait','--wait-timeout',str(args.timeout)], timeout=args.timeout+30)
        print('LAB_FIXTURE local echo only; validator attached to isolated fixture network', flush=True)
    print('Building application images (cached layers reused)', flush=True)
    run(compose+['build','--quiet'], timeout=args.build_timeout)
    run(compose+['up','--detach','--wait','--wait-timeout',str(args.timeout)], timeout=args.timeout+30)
    deadline = time.monotonic()+args.timeout
    last_error = 'not checked'
    while time.monotonic() < deadline:
        try:
            readiness = readiness_check(compose, run, values)
            break
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
            last_error = str(error)
            time.sleep(3)
    else:
        raise RuntimeError('local readiness deadline exceeded: '+last_error)
    after = set(run(compose+['ps','--all','--quiet']).splitlines())
    result = {'commit': args.commit, 'destination': str(target), 'readiness': readiness,
              'config_files_changed': changed, 'containers_created_or_replaced': len(after-before),
              'containers_unchanged': len(before & after), 'container_ids': sorted(after),
              'completed_at': datetime.now(timezone.utc).isoformat(), 'elapsed_seconds': round(time.monotonic()-started,1)}
    atomic_write(STATE, (json.dumps(result,indent=2)+'\n').encode(), 0o600)
    print(f'CONTAINERS created_or_replaced={len(after-before)} unchanged={len(before & after)}', flush=True)
    print('POOL_COUNTS '+json.dumps(readiness['pool_counts'],sort_keys=True), flush=True)
    print('PASS fresh snapshots and local echo through gateway relay', flush=True)
    print(f"LOCAL_READY elapsed={result['elapsed_seconds']}s; client/DNS/public checks pending", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--destination', default='/opt/transparent-gateway')
    parser.add_argument('--config-dir', default='/etc/transparent-gateway/runtime')
    parser.add_argument('--timeout', type=int, default=180)
    parser.add_argument('--build-timeout', type=int, default=1200)
    parser.add_argument('--lab-fixture', action='store_true', help='start local fixture and explicitly attach validator to its network')
    args = parser.parse_args()
    try:
        if args.timeout < 1 or args.build_timeout < 1:
            raise ValueError('timeouts must be positive')
        deploy(args)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.TimeoutExpired) as error:
        # Do not print config values or raw subprocess output.
        print('DEPLOY_FAILED: '+str(error), flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
