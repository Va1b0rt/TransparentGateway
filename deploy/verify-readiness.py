#!/usr/bin/python3
"""Check pipeline locally, or orchestrate a fresh isolated client request over trusted SSH."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import time

from deploy import run
from readiness import check
from service import commands, preflight, settings


def local():
    data=settings()
    values=preflight(data)
    if run(['systemctl','is-active','transparent-gateway.service'])!='active':
        raise ValueError('systemd application is not active')
    compose,_=commands(data)
    deadline=time.monotonic()+20
    while True:
        try: return check(compose,run,values)
        except (OSError,ValueError,RuntimeError,KeyError,TypeError):
            if time.monotonic()>=deadline: raise
            time.sleep(1)


def ssh(host,command,payload=None):
    if not re.fullmatch(r'[a-z_][a-z0-9_-]*@[a-zA-Z0-9.-]+',host):
        raise ValueError('expected user@host')
    process=subprocess.run(['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
        '-o','ConnectTimeout=5',host,command],input=payload,capture_output=True,timeout=90)
    if process.returncode:
        raise RuntimeError('remote readiness check failed; raw output suppressed')
    return json.loads(process.stdout)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json',action='store_true')
    parser.add_argument('--public',action='store_true')
    parser.add_argument('--guard-json',action='store_true')
    parser.add_argument('--gateway',default='tgadmin@192.168.88.114')
    parser.add_argument('--client',default='tgadmin@192.168.88.115')
    args=parser.parse_args()
    if args.guard_json:
        import re
        output=run(['iptables','-L','FORWARD','-n','-v','-x'])
        rows=[line.split() for line in output.splitlines() if 'DROP' in line and '192.168.91.0/24' in line]
        assert len(rows)==1
        print(json.dumps({'dropped':int(rows[0][0])}))
        return
    if args.json:
        print(json.dumps(local()))
        return
    state=ssh(args.gateway,'sudo -n python3 /var/lib/transparent-gateway/source/deploy/verify-readiness.py --json')
    if args.public:
        command='sudo -n python3 /var/lib/transparent-gateway/source/deploy/verify-readiness.py --guard-json'
        before=ssh(args.gateway,command)
        client=ssh(args.client,'python3 -',Path(__file__).with_name('client-public-probe.py').read_bytes())
        after=ssh(args.gateway,command)
        assert after['dropped']>=before['dropped']+3
        assert client['public_exit'] in state['active_exit_ips'] and client['local_exit'] in state['active_exit_ips']
        print('GATEWAY '+args.gateway+' | CLIENT '+args.client)
        print('PASS fresh raw/validated/reserve/active: '+json.dumps(state['pool_counts'],sort_keys=True))
        print('PASS snapshot ages: '+json.dumps(state['snapshot_age_seconds'],sort_keys=True))
        print('PASS validator cycle and lineage; two usable active upstreams')
        print('PASS client default via gateway; no alternate IPv4/IPv6 default')
        print('PASS fresh client UDP DNS via gateway; TCP and verified public HTTPS')
        print('PASS public echo matches active exit [IP hidden]; request_id='+client['public_request_id'])
        print('PASS local transparent echo; request_id='+client['local_request_id'])
        print('PASS direct non-DNS UDP blocked by gateway FORWARD guard')
        print('FULL_CLIENT_READINESS PASS — public headers observed after Cloudflare')
        return
    client=ssh(args.client,'python3 -',Path(__file__).with_name('client-probe.py').read_bytes())
    assert client['peer'] in state['active_exit_ips']
    print('GATEWAY '+args.gateway+' | CLIENT '+args.client)
    print('PASS raw/validated/reserve/active: fresh, nonempty; counts='+json.dumps(state['pool_counts'],sort_keys=True))
    print('PASS validator success and pool lineage: '+json.dumps(state['pipeline'],sort_keys=True))
    print('PASS usable active upstream and local relay CONNECT/echo')
    print('PASS client route: 192.168.91.2 -> 192.168.91.1; no alternative default')
    print('PASS transparent client TCP/echo: exit='+client['peer']+'; request_id='+client['request_id'])
    print('LAB_READINESS PASS — public HTTPS echo and client DNS checks pending stages 7–8')


if __name__=='__main__':
    try: main()
    except Exception as error:
        print('READINESS FAILED: '+type(error).__name__+'; details suppressed',flush=True)
        raise SystemExit(1)
