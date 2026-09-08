#!/usr/bin/python3
"""Install supervision for the already deployed immutable release; start via systemd."""
import argparse
import json
import os
from pathlib import Path
import re

from deploy import STATE, atomic_write, run

ROOT=Path('/opt/transparent-gateway')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--commit',required=True)
    parser.add_argument('--lab-fixture',action='store_true')
    args=parser.parse_args()
    if os.geteuid()!=0 or not re.fullmatch('[0-9a-f]{40}',args.commit):
        raise ValueError('root and full commit SHA required')
    if run(['git','-C',str(ROOT),'rev-parse','HEAD'])!=args.commit:
        raise ValueError('deploy requested commit before installing service')
    if json.loads(STATE.read_text())['commit']!=args.commit:
        raise ValueError('successful deployment of this commit required')
    run(['systemctl','is-enabled','systemd-networkd-wait-online.service'])
    settings={'commit':args.commit,'destination':str(ROOT),'lab_fixture':args.lab_fixture}
    config_changed=atomic_write(Path('/etc/transparent-gateway/service.json'),(json.dumps(settings)+'\n').encode(),0o600)
    unit_changed=atomic_write(Path('/etc/systemd/system/transparent-gateway.service'),
                              (ROOT/'deploy/transparent-gateway.service').read_bytes(),0o644)
    run(['systemd-analyze','verify','/etc/systemd/system/transparent-gateway.service'])
    run(['python3',str(ROOT/'deploy/service.py'),'check'])
    run(['systemctl','daemon-reload'])
    run(['systemctl','enable','transparent-gateway.service'])
    run(['systemctl','restart' if config_changed or unit_changed else 'start','transparent-gateway.service'],timeout=440)
    run(['systemctl','is-active','transparent-gateway.service'])
    print('SYSTEMD_INSTALLED PASS: enabled, active; local readiness passed; release '+args.commit)


if __name__=='__main__':
    try:
        main()
    except Exception as error:
        print('SYSTEMD_INSTALL FAILED: '+type(error).__name__+'; details suppressed',flush=True)
        raise SystemExit(1)
