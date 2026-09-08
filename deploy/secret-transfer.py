#!/usr/bin/python3
"""Deliver a validated runtime set through SSH stdin; never print its contents."""
import argparse
import base64
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

from deploy import FILES, atomic_write, validate

LIMIT = 1024 * 1024
CANONICAL = Path('/etc/transparent-gateway/runtime')


def document(root):
    result = {}
    if root.is_symlink() or root.stat().st_uid != 0 or stat.S_IMODE(root.stat().st_mode) != 0o700:
        raise ValueError('source directory must be root-owned mode 0700')
    for name in FILES:
        path = root/name
        if path.parent.is_symlink() or path.is_symlink() or not path.is_file():
            raise ValueError('regular files and directories required')
        info = path.stat()
        if info.st_uid != 0 or info.st_mode & 0o007 or info.st_mode & 0o022:
            raise ValueError('source permissions are too broad')
        result[name] = base64.b64encode(path.read_bytes()).decode()
    payload = json.dumps(result).encode()
    if len(payload) > LIMIT:
        raise ValueError('runtime set exceeds 1 MiB')
    return payload


def receive():
    payload = sys.stdin.buffer.read(LIMIT+1)
    if len(payload) > LIMIT:
        raise ValueError('runtime set exceeds 1 MiB')
    data = json.loads(payload)
    if set(data) != set(FILES):
        raise ValueError('runtime set must contain exactly the declared files')
    parent = CANONICAL.parent
    lock_path = Path('/var/lib/transparent-gateway/deployment.lock')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a') as lock, tempfile.TemporaryDirectory(prefix='.incoming-', dir=parent) as tmp:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        staging = Path(tmp)
        for name in FILES:
            atomic_write(staging/name, base64.b64decode(data[name], validate=True),
                         0o600 if name == '.env' else 0o640, 0 if name == '.env' else 65532)
        validate(staging, json.loads((parent/'host.json').read_text()))
        if CANONICAL.is_symlink():
            raise ValueError('canonical directory cannot be a symlink')
        CANONICAL.mkdir(mode=0o700, exist_ok=True)
        os.chown(CANONICAL, 0, 0)
        CANONICAL.chmod(0o700)
        # Check all target parents before any file replacement. The deployment lock
        # prevents a deployment observing a partially installed set. A power loss
        # is recovered by resending the same validated set before deployment.
        for name in FILES:
            folder = (CANONICAL/name).parent
            if folder.is_symlink() or (CANONICAL/name).is_symlink():
                raise ValueError('symlink in destination')
        changed = 0
        for name in FILES:
            target = CANONICAL/name
            if name != '.env':
                target.parent.mkdir(mode=0o750, exist_ok=True)
                os.chown(target.parent, 0, 65532)
                target.parent.chmod(0o750)
            changed += atomic_write(target, (staging/name).read_bytes(),
                                    0o600 if name == '.env' else 0o640, 0 if name == '.env' else 65532)
        print(f'SECRET_DELIVERY PASS files={len(FILES)} changed={changed}; validated, root-owned; values hidden')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['send', 'receive'])
    parser.add_argument('--host')
    parser.add_argument('--directory', type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError('run as root')
    os.umask(0o077)
    if args.action == 'receive':
        receive()
    else:
        if not args.host or not re.fullmatch(r'[a-z_][a-z0-9_-]*@[a-zA-Z0-9.-]+', args.host) or not args.directory:
            raise ValueError('supply user@host and a protected source directory')
        payload = document(args.directory)
        result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            args.host, 'sudo -n python3 /var/lib/transparent-gateway/source/deploy/secret-transfer.py receive'],
            input=payload, capture_output=True)
        if result.returncode or not result.stdout.startswith(b'SECRET_DELIVERY PASS '):
            raise RuntimeError('remote delivery failed; no payload or subprocess output displayed')
        print(result.stdout.decode().strip())


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Parsing exceptions can contain arbitrary supplied data: print type only.
        print('SECRET_DELIVERY FAILED: '+type(error).__name__, file=sys.stderr)
        raise SystemExit(1)
