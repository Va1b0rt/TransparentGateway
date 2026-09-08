#!/usr/bin/env python3
"""Generate reviewed cloud-config from a clean, committed checkout; no secrets."""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument('hostname')
parser.add_argument('public_keys', type=Path)
parser.add_argument('output', type=Path)
args = parser.parse_args()
root = Path(__file__).resolve().parents[2]

def git(*arguments):
    return subprocess.check_output(['git', '-C', str(root), *arguments], text=True).strip()

if git('status', '--porcelain', '--untracked-files=no'):
    parser.error('commit the bootstrap source before rendering')
commit = git('rev-parse', 'HEAD')
if not re.fullmatch(r'[a-z][a-z0-9-]{0,62}', args.hostname):
    parser.error('invalid hostname')
keys = [line.strip() for line in args.public_keys.read_text().splitlines() if line.strip()]
if not keys or any(not k.startswith(('ssh-ed25519 ', 'ssh-rsa ', 'ecdsa-sha2-')) for k in keys):
    parser.error('expected approved SSH public keys')
bootstrap = (root / 'deploy/cloud-init/bootstrap.py').read_text()
unit = (root / 'deploy/cloud-init/tg-bootstrap.service').read_text()
packages = ['ca-certificates', 'curl', 'git', 'python3', 'iproute2', 'iptables', 'bind9-dnsutils', 'docker.io', 'docker-compose', 'qemu-guest-agent']
network = {
    'required_packages': packages,
    'interfaces': {
        'eth0': str(ipaddress.IPv4Interface(os.getenv('MGMT_CIDR', '192.168.88.114/24'))),
        'eth1': str(ipaddress.IPv4Interface(os.getenv('CLIENT_CIDR', '192.168.91.1/24'))),
        'eth2': str(ipaddress.IPv4Interface(os.getenv('LAB_CIDR', '10.10.20.2/24'))),
    },
    'default_gateway': str(ipaddress.IPv4Address(os.getenv('MGMT_GATEWAY', '192.168.88.1'))),
}
source = {'commit': commit, 'bootstrap_sha256': hashlib.sha256(bootstrap.encode()).hexdigest()}
lines = [
    '#cloud-config',
    'hostname: ' + args.hostname,
    'manage_etc_hosts: true',
    'preserve_hostname: false',
    'ssh_pwauth: false',
    'disable_root: true',
    'users:',
    '  - name: tgadmin',
    '    groups: [sudo]',
    '    shell: /bin/bash',
    '    lock_passwd: true',
    "    sudo: ['ALL=(ALL) NOPASSWD:ALL']",
    '    ssh_authorized_keys:',
    *['      - ' + json.dumps(key) for key in keys],
    'package_update: true',
    'package_upgrade: true',
    'package_reboot_if_required: false',
    'packages:',
    *['  - ' + p for p in packages],
    'write_files:',
]
for path, permissions, content in [
    ('/usr/local/sbin/tg-bootstrap', '0755', bootstrap),
    ('/etc/systemd/system/tg-bootstrap.service', '0644', unit),
    ('/etc/transparent-gateway/host.json', '0644', json.dumps(network, indent=2)),
    ('/usr/local/lib/transparent-gateway/bootstrap-source.json', '0644', json.dumps(source, indent=2)),
]:
    lines += ['  - path: ' + path, '    owner: root:root', "    permissions: '" + permissions + "'", '    content: |']
    lines += ['      ' + line for line in content.splitlines()]
lines += [
    'runcmd:',
    '  - [systemctl, daemon-reload]',
    '  - [systemctl, enable, --now, docker.service]',
    '  - [systemctl, start, qemu-guest-agent.service]',
    '  - [systemctl, start, tg-bootstrap.service]',
    '',
]
with args.output.open('x') as stream:
    stream.write('\n'.join(lines))
args.output.chmod(0o600)
print(f'Wrote stage 2 cloud-config from commit {commit}; no application secrets supplied.')
