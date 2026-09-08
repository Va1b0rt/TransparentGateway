#!/usr/bin/python3
"""Check host prerequisites and record bootstrap completion, without deployment."""
import argparse
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import tempfile

CONFIG = Path('/etc/transparent-gateway/host.json')
SOURCE = Path('/usr/local/lib/transparent-gateway/bootstrap-source.json')
STATE = Path('/var/lib/transparent-gateway/bootstrap.json')


def run(*command):
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError(f'{command[0]} prerequisite check failed (exit {result.returncode})')
    return result.stdout.strip()


def check():
    if os.geteuid() != 0:
        raise RuntimeError('run with sudo')
    release = {}
    for line in Path('/etc/os-release').read_text().splitlines():
        if '=' in line and not line.startswith('#'):
            key, value = line.split('=', 1)
            release[key] = shlex.split(value)[0]
    if release.get('ID') != 'debian' or release.get('VERSION_ID') != '13':
        raise RuntimeError('Debian 13 is required')
    print('PASS Debian 13', flush=True)

    config = json.loads(CONFIG.read_text())
    addresses = {entry['ifname']: entry for entry in json.loads(run('ip', '-j', '-4', 'address'))}
    for name, expected in config['interfaces'].items():
        expected = str(ipaddress.IPv4Interface(expected))
        interface = addresses.get(name, {})
        actual = {f"{a['local']}/{a['prefixlen']}" for a in interface.get('addr_info', [])}
        if expected not in actual or interface.get('operstate') != 'UP':
            raise RuntimeError(f'interface {name} does not match expected network')
        print(f'PASS {name} {expected}', flush=True)
    routes = json.loads(run('ip', '-j', '-4', 'route', 'show', 'default'))
    if len(routes) != 1 or routes[0].get('dev') != 'eth0' or routes[0].get('gateway') != config['default_gateway']:
        raise RuntimeError('expected one management default route on eth0')
    print('PASS default route via management', flush=True)

    docker = run('docker', 'info', '--format', '{{.ServerVersion}}')
    compose = run('docker', 'compose', 'version', '--short')
    if int(compose.lstrip('v').split('.')[0]) < 2:
        raise RuntimeError('Compose v2 or newer is required')
    print(f'PASS Docker {docker}; Compose {compose}', flush=True)
    if run('systemctl', 'is-active', 'qemu-guest-agent.service') != 'active':
        raise RuntimeError('QEMU guest agent is not active')
    print('PASS QEMU guest agent', flush=True)

    source = json.loads(SOURCE.read_text())
    digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if digest != source['bootstrap_sha256']:
        raise RuntimeError('bootstrap file does not match its source manifest')
    print(f"PASS bootstrap source {source['commit'][:12]}", flush=True)
    return {
        'status': 'HOST_READY',
        'application_deployment': 'pending',
        'hostname': socket.gethostname(),
        'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
        'source_commit': source['commit'],
        'bootstrap_sha256': digest,
        'docker_version': docker,
        'compose_version': compose,
        'interfaces': config['interfaces'],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='read-only prerequisite and recorded state check')
    args = parser.parse_args()
    try:
        result = check()
        if args.check:
            recorded = json.loads(STATE.read_text())
            if recorded.get('status') != 'HOST_READY' or recorded.get('source_commit') != result['source_commit'] or recorded.get('bootstrap_sha256') != result['bootstrap_sha256']:
                raise RuntimeError('no matching successful bootstrap record')
            print(f"BOOTSTRAP_COMPLETED {recorded['completed_at']}")
        else:
            STATE.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
            result['completed_at'] = datetime.now(timezone.utc).isoformat()
            # Atomic, root-owned state. No config values or secrets enter journal.
            with tempfile.NamedTemporaryFile(mode='w', dir=STATE.parent, delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(result, stream, indent=2)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o640)
            os.replace(temporary, STATE)
        print('HOST_READY; application deployment pending (stage 3)', flush=True)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f'BOOTSTRAP_FAILED: {error}', flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
