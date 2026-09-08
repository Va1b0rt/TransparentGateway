#!/usr/bin/python3
"""Create explicit, non-production config for the local fixture. Never overwrite."""
import json
import os
from pathlib import Path

assert os.geteuid() == 0, 'run with sudo'
root = Path(__file__).resolve().parents[2]
destination = Path('/etc/transparent-gateway/runtime')
if destination.exists():
    raise SystemExit('Config directory already exists; inspect it instead of overwriting.')
values = {}
for line in (root / '.env.example').read_text().splitlines():
    if line and not line.startswith('#'):
        key, value = line.split('=', 1)
        values[key] = value
values.update(CLIENT_CIDR='192.168.91.0/24', POOL_SIZE='1', RESERVE_SIZE='4',
              MAX_UNKNOWN_ASN_RATIO='1', VALIDATION_CONCURRENCY='2',
              VALIDATION_INTERVAL_SECONDS='15', FETCH_INTERVAL_SECONDS='15', RANK_INTERVAL_SECONDS='5',
              ANONYMITY_CHECK_URL='http://172.30.40.10:8000/')
destination.mkdir(mode=0o700)
for name in ['inventory', 'secrets', 'provider-secrets']:
    (destination / name).mkdir(mode=0o750)
    os.chown(destination / name, 0, 65532)
files = {
    '.env': ''.join(f'{key}={value}\n' for key, value in values.items()),
    'inventory/connectors.json': json.dumps({'allowed_connector_types': ['jsonl_inventory'], 'active_connectors': [
        {'type': 'jsonl_inventory', 'name': 'local-fixture', 'path': '/inventory/upstreams.jsonl'}]})+'\n',
    'inventory/upstreams.jsonl': json.dumps({'host':'172.30.40.11', 'port':3128, 'protocol':'http', 'source':'local-fixture'})+'\n',
    'secrets/proxy-credentials.json': '{}\n',
    'provider-secrets/api-token': '',
}
for name, content in files.items():
    path = destination / name
    path.write_text(content)
    path.chmod(0o600 if name == '.env' else 0o640)
    os.chown(path, 0, 0 if name == '.env' else 65532)
print('Created lab config; empty credential store; no provider token. External readiness is not enabled.')
