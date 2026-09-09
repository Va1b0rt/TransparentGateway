#!/usr/bin/python3
"""Merge a verified external handoff into protected lab runtime without displaying values."""
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from deploy import atomic_write, config_files, read_env, validate


def main():
    assert os.geteuid()==0
    os.umask(0o077)
    config=Path('/etc/transparent-gateway/runtime')
    handoff=Path('/etc/transparent-gateway/external-upstream')
    host=json.loads(Path('/etc/transparent-gateway/host.json').read_text())
    assert host['interfaces']['eth1']=='192.168.91.1/24'
    for name in ('config.json','credentials.json','ca.crt'):
        f=handoff/name
        assert not f.is_symlink() and f.stat().st_uid==0 and f.stat().st_mode & 0o777==0o600
    external=json.loads((handoff/'config.json').read_text())
    assert ipaddress.ip_address(external['server_ip']).is_global
    assert external['echo_host']=='echo.devdays.net.ua'
    credential=json.loads((handoff/'credentials.json').read_text())
    with Path('/var/lib/transparent-gateway/deployment.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with tempfile.TemporaryDirectory(dir=config.parent,prefix='.public-') as folder:
            staging=Path(folder)
            for name in config_files(config):
                atomic_write(staging/name,(config/name).read_bytes(),0o600 if name=='.env' else 0o640,0 if name=='.env' else 65532)
            values=read_env(staging/'.env')
            values.update(ANONYMITY_CHECK_URL='https://echo.devdays.net.ua/',POOL_SIZE='2',RESERVE_SIZE='4',
                MAX_UNKNOWN_ASN_RATIO='0.5',LAB_LOCAL_ECHO_ENABLED='1',VALIDATION_INTERVAL_SECONDS='30',
                DNS_DEFAULT_RESOLVER_IPS='1.1.1.1,1.0.0.1,192.168.91.1')
            atomic_write(staging/'.env',''.join(f'{k}={v}\n' for k,v in values.items()).encode(),0o600)
            creds=json.loads((staging/'secrets/proxy-credentials.json').read_text())
            assert 'lab-proxy' in creds
            creds['public-proxy']={**credential,'ca_file':'/run/secrets/upstream-ca.crt'}
            atomic_write(staging/'secrets/proxy-credentials.json',(json.dumps(creds)+'\n').encode(),0o640,65532)
            atomic_write(staging/'secrets/upstream-ca.crt',(handoff/'ca.crt').read_bytes(),0o640,65532)
            entries=[{'host':'172.30.40.11','port':3128,'protocol':'http','source':'local-fixture','credential_ref':'lab-proxy'},
                {'host':external['server_ip'],'port':3129,'protocol':'https','source':'owned-public-proxy','credential_ref':'public-proxy'}]
            atomic_write(staging/'inventory/upstreams.jsonl',''.join(json.dumps(e)+'\n' for e in entries).encode(),0o640,65532)
            validate(staging,host)
            backup=Path('/var/lib/transparent-gateway/runtime-before-public')
            if not backup.exists():shutil.copytree(config,backup)
            changed=0
            for name in config_files(staging):
                changed+=atomic_write(config/name,(staging/name).read_bytes(),0o600 if name=='.env' else 0o640,0 if name=='.env' else 65532)
    print('PUBLIC_CONFIG PASS: two explicit upstreams; scoped CA; protected files; changed='+str(changed))


if __name__=='__main__':
    try:main()
    except Exception as error:
        print('PUBLIC_CONFIG FAILED: '+type(error).__name__+'; values hidden')
        raise SystemExit(1)
