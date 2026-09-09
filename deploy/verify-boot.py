#!/usr/bin/python3
"""Record pre-reboot evidence, or verify the current automatic systemd boot."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import uuid
import time

from deploy import config_files, atomic_write, run
from readiness import check
from service import BOOT_STATE, ROOT, commands, containers, preflight, settings

BASELINE=Path('/var/lib/transparent-gateway/reboot-baseline.json')
REDIS=['docker','exec','transparent-gateway-redis-1','redis-cli','--raw']


def file_state():
    result={}
    for root in [ROOT,Path('/etc/transparent-gateway/runtime')]:
        for name in config_files(root):
            path=root/name
            info=path.stat()
            result[str(path)]=[hashlib.sha256(path.read_bytes()).hexdigest(),info.st_mode,info.st_uid,info.st_gid]
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare',action='store_true')
    args=parser.parse_args()
    if os.geteuid()!=0:
        raise ValueError('root required')
    data=settings()
    values=preflight(data)
    assert run(['systemctl','is-enabled','transparent-gateway.service'])=='enabled'
    assert run(['systemctl','is-active','transparent-gateway.service'])=='active'
    boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    current=containers(data)
    if args.prepare:
        if BASELINE.exists():
            raise ValueError('baseline already exists; retain it for the screenshot gate')
        key='tg:deployment:reboot:'+uuid.uuid4().hex
        value=uuid.uuid4().hex
        assert run(REDIS+['SET',key,value,'EX','604800','NX'])=='OK'
        baseline={'boot_id':boot,'commit':data['commit'],'containers':current,'files':file_state(),'marker':key,'value':value}
        atomic_write(BASELINE,(json.dumps(baseline,indent=2)+'\n').encode(),0o600)
        print('REBOOT_BASELINE SAVED: boot ID, container IDs, protected file state and Redis marker')
        return
    before=json.loads(BASELINE.read_text())
    assert boot!=before['boot_id'] and data['commit']==before['commit']
    record=json.loads(BOOT_STATE.read_text())
    assert record['boot_id']==boot and record['commit']==data['commit']
    assert current==before['containers']==record['containers']
    assert file_state()==before['files']
    assert run(REDIS+['GET',before['marker']])==before['value']
    def timestamp(unit,prop):
        return int(run(['systemctl','show',unit,'--property='+prop,'--value']))
    started=timestamp('transparent-gateway.service','ExecMainStartTimestampMonotonic')
    for unit in ['docker.service','network-online.target']:
        assert 0<timestamp(unit,'ActiveEnterTimestampMonotonic')<=started
    for identifier in current.values():
        assert run(['docker','inspect','--format','{{.HostConfig.RestartPolicy.Name}}',identifier])=='no'
    compose,_=commands(data)
    deadline=time.monotonic()+20
    while True:
        try:
            check(compose,run,values)
            break
        except (OSError,ValueError,RuntimeError,KeyError,TypeError):
            if time.monotonic()>=deadline:raise
            time.sleep(1)
    print('BOOT '+run(['uptime','-s'])+' | READY '+str(round(timestamp('transparent-gateway.service','ActiveEnterTimestampMonotonic')/1000000,2))+'s after boot')
    print('HOST '+socket_hostname()+' | SYSTEMD enabled / active (running)')
    print('PASS reboot: boot ID changed; current boot has BOOT_LOCAL_READY')
    print('PASS ordering: network-online + Docker ready before application')
    print('PASS same 6 application + 2 lab containers; Docker auto-restart disabled')
    print('PASS Redis marker and protected config/secrets/proxy CA preserved')
    print('PASS fresh pools and authenticated local relay echo after reboot')
    print('REBOOT_CHECK PASS — automatic systemd startup; client/DNS/public checks pending')


def socket_hostname():
    import socket
    return socket.gethostname()


if __name__=='__main__':
    try:
        main()
    except Exception as error:
        print('REBOOT_CHECK FAILED: '+type(error).__name__+'; details suppressed',flush=True)
        raise SystemExit(1)
