import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'deploy'))
spec = importlib.util.spec_from_file_location('tg_deploy', ROOT/'deploy/deploy.py')
deployment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployment)


def test_env_is_literal_and_rejects_duplicate_keys(tmp_path):
    path = tmp_path/'.env'
    path.write_text('CLIENT_CIDR=192.168.91.0/24\nCLIENT_CIDR=192.168.88.0/24\n')
    with pytest.raises(ValueError, match='duplicate'):
        deployment.read_env(path)
    path.write_text('X=$(touch /tmp/never-run)\n')
    with pytest.raises(ValueError, match='literal'):
        deployment.read_env(path)


@pytest.fixture
def runtime_config(tmp_path):
    if os.geteuid() != 0:
        pytest.skip('file ownership deployment checks require root')
    values = {}
    for line in (ROOT/'.env.example').read_text().splitlines():
        if line and not line.startswith('#'):
            key, value = line.split('=', 1)
            values[key] = value
    values['CLIENT_CIDR'] = '192.168.91.0/24'
    documents = {
        '.env': ''.join(f'{k}={v}\n' for k,v in values.items()),
        'inventory/connectors.json': json.dumps({'allowed_connector_types':['jsonl_inventory'],
            'active_connectors':[{'type':'jsonl_inventory','name':'test','path':'/inventory/upstreams.jsonl'}]}),
        'inventory/upstreams.jsonl': '{"host":"192.0.2.1","port":3128,"protocol":"http","credential_ref":"provider"}\n',
        'provider-secrets/api-token': '',
        'secrets/proxy-credentials.json': '{"provider":{"username":"test","password":"not-a-real-secret"}}',
    }
    for name, text in documents.items():
        path = tmp_path/name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(text)
        path.chmod(0o600 if name=='.env' else 0o640)
        os.chown(path,0,0 if name=='.env' else 65532)
    host = {'interfaces':{'eth0':'192.168.88.114/24','eth1':'192.168.91.1/24','eth2':'10.10.20.2/24'}}
    return tmp_path, host


def test_management_cannot_be_intercepted(runtime_config):
    root, host = runtime_config
    deployment.validate(root, host)
    path = root/'.env'
    path.write_text(path.read_text().replace('CLIENT_CIDR=192.168.91.0/24','CLIENT_CIDR=192.168.88.0/24'))
    with pytest.raises(ValueError,match='exclude management'):
        deployment.validate(root,host)


def test_missing_credentials_and_world_readable_store_rejected(runtime_config):
    root, host = runtime_config
    path = root/'secrets/proxy-credentials.json'
    path.chmod(0o644)
    with pytest.raises(ValueError,match='permissions'):
        deployment.validate(root,host)
    path.chmod(0o640)
    path.write_text('{}')
    with pytest.raises(ValueError,match='reference is missing'):
        deployment.validate(root,host)


def test_unchanged_config_preserves_inode_and_mtime(tmp_path):
    if os.geteuid() != 0:
        pytest.skip('root-owned deployment file')
    target = tmp_path/'runtime.json'
    assert deployment.atomic_write(target,b'{}\n',0o600)
    before=target.stat()
    assert not deployment.atomic_write(target,b'{}\n',0o600)
    after=target.stat()
    assert (before.st_ino,before.st_mtime_ns)==(after.st_ino,after.st_mtime_ns)
    link = tmp_path/'link'
    link.symlink_to(target)
    with pytest.raises(ValueError,match='symlink'):
        deployment.atomic_write(link,b'changed',0o600)
