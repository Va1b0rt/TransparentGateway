#!/usr/bin/python3
"""Repeat the real lab deployment and assert preservation of the running state."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import uuid

from deploy import FILES, SAFE_ENV, run

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source', required=True)
parser.add_argument('--commit', required=True)
args = parser.parse_args()
if os.geteuid() != 0 or not re.fullmatch('[0-9a-f]{40}', args.commit):
    raise SystemExit('Run with sudo and an explicit full commit SHA.')
root = Path('/opt/transparent-gateway')
if run(['git','-C',str(root),'rev-parse','HEAD']) != args.commit:
    raise SystemExit('Deploy this commit once before testing an identical repeat.')
redis = ['docker','exec','transparent-gateway-redis-1','redis-cli','--raw']


def snapshot():
    info = run(redis+['INFO','server'])
    run_id = next(line.split(':',1)[1] for line in info.splitlines() if line.startswith('run_id:'))
    containers = run(['docker','ps','--all','--filter','label=com.docker.compose.project=transparent-gateway','--format','{{.ID}} {{.Names}}']).splitlines()
    fixture = run(['docker','ps','--all','--filter','label=com.docker.compose.project=tg-lab','--format','{{.ID}} {{.Names}}']).splitlines()
    files = {}
    for name in FILES:
        path=root/name
        info=path.stat()
        files[name]=[hashlib.sha256(path.read_bytes()).hexdigest(),info.st_ino,info.st_mtime_ns,info.st_mode,info.st_uid,info.st_gid]
    return {'containers':sorted(containers),'fixture':sorted(fixture),'redis_run_id':run_id,
            'files':files,'iptables':run(['iptables-save']), 'policy_routes':run(['ip','-4','rule','show']),
            'tproxy_routes':run(['ip','-4','route','show','table','100'])}


marker = 'tg:deployment:repeat:'+uuid.uuid4().hex
value = uuid.uuid4().hex
try:
    before=snapshot()
    if len(before['containers']) != 6:
        raise RuntimeError('expected six application containers before repeat')
    if run(redis+['SET',marker,value,'EX','600','NX']) != 'OK':
        raise RuntimeError('could not create verification marker')
    command=['python3',str(Path(__file__).with_name('deploy.py')),'--source',args.source,'--commit',args.commit,'--lab-fixture']
    subprocess.run(command,env=SAFE_ENV,check=True)
    after=snapshot()
    if before != after:
        changed=[key for key in before if before[key]!=after[key]]
        raise RuntimeError('repeat changed: '+', '.join(changed))
    if run(redis+['GET',marker]) != value:
        raise RuntimeError('Redis data marker was lost')
    print('PASS same 6 application and 2 fixture containers',flush=True)
    print('PASS Redis process and data preserved',flush=True)
    print('PASS config bytes/inodes/mtime/permissions unchanged',flush=True)
    print('PASS iptables and policy routing unchanged',flush=True)
    print('IDEMPOTENT_REPEAT PASS',flush=True)
except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as error:
    print('IDEMPOTENT_REPEAT FAILED: '+str(error),flush=True)
    raise SystemExit(1)
finally:
    try:
        run(redis+['DEL',marker])
    except RuntimeError:
        pass  # Namespaced marker expires after ten minutes even if Redis is unavailable.
