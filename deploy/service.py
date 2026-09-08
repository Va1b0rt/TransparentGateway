#!/usr/bin/python3
"""Start the installed release and supervise containers without logging their output."""
import fcntl
import json
import os
from pathlib import Path
import socket
import sys
import time

from deploy import STATE, atomic_write, run, validate
from readiness import check as ready

ROOT = Path('/opt/transparent-gateway')
SETTINGS = Path('/etc/transparent-gateway/service.json')
BOOT_STATE = Path('/var/lib/transparent-gateway/service-boot.json')


def settings():
    info = SETTINGS.lstat()
    if SETTINGS.is_symlink() or info.st_uid != 0 or info.st_mode & 0o777 != 0o600:
        raise ValueError('invalid service settings ownership or permissions')
    data = json.loads(SETTINGS.read_text())
    if data['destination'] != str(ROOT) or not isinstance(data['lab_fixture'], bool):
        raise ValueError('unexpected service destination or fixture setting')
    return data


def commands(data):
    compose = ['docker','compose','--project-name','transparent-gateway','--project-directory',str(ROOT),
               '--env-file',str(ROOT/'.env'),'-f',str(ROOT/'docker-compose.yml')]
    fixture = ['docker','compose','--project-name','tg-lab','-f',str(ROOT/'deploy/lab/compose.yml')]
    if data['lab_fixture']:
        compose += ['-f',str(ROOT/'deploy/lab/gateway.override.yml')]
    return compose, fixture


def network(host):
    addresses = {row['ifname']:row for row in json.loads(run(['ip','-j','-4','address']))}
    for name, cidr in host['interfaces'].items():
        row = addresses.get(name,{})
        if row.get('operstate') != 'UP' or cidr not in {f"{a['local']}/{a['prefixlen']}" for a in row.get('addr_info',[])}:
            raise ValueError('declared NIC not ready')
    routes = json.loads(run(['ip','-j','-4','route','show','default']))
    if len(routes)!=1 or routes[0].get('dev')!='eth0' or routes[0].get('gateway')!=host['default_gateway']:
        raise ValueError('declared default route not ready')


def preflight(data):
    if run(['git','-C',str(ROOT),'rev-parse','HEAD']) != data['commit']:
        raise ValueError('installed release differs from service pin; reinstall service settings')
    if run(['git','-C',str(ROOT),'status','--porcelain','--untracked-files=no']):
        raise ValueError('installed release has tracked changes')
    host = json.loads(Path('/etc/transparent-gateway/host.json').read_text())
    deadline = time.monotonic()+90
    while True:
        try:
            network(host)
            break
        except ValueError:
            if time.monotonic()>=deadline:
                raise
            time.sleep(2)
    values = validate(ROOT,host)
    compose, fixture = commands(data)
    for command in [compose]+([fixture] if data['lab_fixture'] else []):
        run(command+['config','--quiet'])
        rendered = json.loads(run(command+['config','--format','json']))
        if any(service.get('restart','no') != 'no' for service in rendered['services'].values()):
            raise ValueError('systemd supervision requires Docker restart policies disabled')
    return values


def notify(message):
    address = os.environ.get('NOTIFY_SOCKET')
    if not address:
        raise ValueError('service must be started by systemd')
    if address.startswith('@'):
        address = '\0'+address[1:]
    with socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM) as sock:
        sock.connect(address)
        sock.sendall(message.encode())


def containers(data):
    result = {}
    for project, count in [('transparent-gateway',6)]+([('tg-lab',2)] if data['lab_fixture'] else []):
        ids = run(['docker','ps','-aq','--filter','label=com.docker.compose.project='+project]).splitlines()
        if len(ids)!=count:
            raise RuntimeError('expected application containers missing')
        rows = json.loads(run(['docker','inspect',*ids]))
        for row in rows:
            if not row['State']['Running'] or row['State'].get('Health',{}).get('Status','healthy')=='unhealthy':
                raise RuntimeError('application container stopped or unhealthy')
            result[row['Name'].lstrip('/')] = row['Id']
    return result


def serve(data):
    with (STATE.parent/'deployment.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
        values = preflight(data)
        compose, fixture = commands(data)
        for command in ([fixture] if data['lab_fixture'] else [])+[compose]:
            run(command+['up','--detach','--no-build','--pull','never','--wait','--wait-timeout','180'],timeout=200)
        deadline = time.monotonic()+180
        while True:
            try:
                result = ready(compose,run,values)
                break
            except (OSError,ValueError,RuntimeError,KeyError,TypeError):
                if time.monotonic()>=deadline:
                    raise RuntimeError('local readiness deadline exceeded')
                time.sleep(3)
        record = {'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                  'commit':data['commit'],'ready_at':time.time(),'containers':containers(data),'readiness':result}
        atomic_write(BOOT_STATE,(json.dumps(record,indent=2)+'\n').encode(),0o600)
        print('BOOT_LOCAL_READY: 6 services; fresh pools; authenticated relay echo; client/DNS/public pending',flush=True)
        notify('READY=1\nSTATUS=Local ready; supervising containers; client/DNS/public checks pending')
    while True:
        time.sleep(15)
        with (STATE.parent/'deployment.lock').open('a') as lock:
            try:
                fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            containers(data)


def stop(data):
    for project in ['transparent-gateway']+(['tg-lab'] if data['lab_fixture'] else []):
        ids = run(['docker','ps','-q','--filter','label=com.docker.compose.project='+project]).splitlines()
        if ids:
            run(['docker','stop','--time','20',*ids],timeout=50)
    print('STOPPED: application containers; data volumes retained',flush=True)


if __name__=='__main__':
    try:
        data = settings()
        action = sys.argv[1]
        if action=='check':
            preflight(data)
            print('PREFLIGHT PASS: pinned release, three NICs, secrets and Compose config',flush=True)
        elif action=='serve':
            serve(data)
        elif action=='stop':
            stop(data)
        else:
            raise ValueError('invalid service action')
    except Exception as error:
        print('SERVICE_FAILED: '+type(error).__name__+'; details suppressed',flush=True)
        raise SystemExit(1)
