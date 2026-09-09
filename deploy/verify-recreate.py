#!/usr/bin/python3
"""Read-only screenshot evidence: recorded fresh clone plus live full boot checks."""
import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    assert os.geteuid() == 0
    info = args.evidence.stat()
    assert info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o700
    record = args.evidence/'result.json'
    info = record.stat()
    assert info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o600
    data = json.loads(record.read_text())
    assert data['status'] == 'PASS' and data['manual_repairs'] == 0
    assert data['steps'] == ['preflight','clean_clone','cloud_init','secret_delivery','deployment',
                             'first_client_readiness','automatic_reboot']
    assert data['template'] == 9001 and data['vmid'] == 216
    assert data['gateway'] == 'tgadmin@192.168.88.116' and data['client'] == 'tgadmin@192.168.88.115'
    assert data['clean_state_verified'] and data['template_unchanged'] and data['old_vm_stopped']
    assert data['runtime_files'] == 6
    def ssh(host, command):
        p = subprocess.run(['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=5',host,command],capture_output=True,text=True,timeout=30)
        assert p.returncode == 0
        return p.stdout.strip()
    identity = ssh(data['gateway'], 'hostname; cat /etc/machine-id; cat /proc/sys/kernel/random/boot_id; sudo -n git -C /opt/transparent-gateway rev-parse HEAD').splitlines()
    assert identity == [data['hostname'],data['new_machine_id'],data['reboot_id'],data['commit']]
    assert data['new_machine_id'] != data['old_machine_id']
    assert ssh('root@192.168.88.2','qm status 214') == 'status: stopped'
    assert ssh('root@192.168.88.2','qm config 9001') == (args.evidence/'template-before.txt').read_text().strip()
    print('TEMPLATE 9001 -> NEW VM 216 / '+data['hostname']+' / 192.168.88.116',flush=True)
    print('COMMIT '+data['commit'],flush=True)
    print('PASS fresh machine identity; empty application state before deployment; six protected runtime files',flush=True)
    print('PASS first boot: cloud-init, deployment, systemd, secrets and client DNS/TCP/local/public echo',flush=True)
    print('TIMING clone='+str(data['clone_seconds'])+'s | deployment='+str(data['deployment_seconds'])+'s | first-ready='+str(data['first_ready_seconds'])+'s',flush=True)
    p = subprocess.run([sys.executable,str(ROOT/'verify-full-boot.py'),'--gateway',data['gateway'],'--client',data['client']],capture_output=True,text=True,timeout=360)
    assert p.returncode == 0 and 'FULL_REBOOT_CHECK PASS' in p.stdout
    print(p.stdout.strip(),flush=True)
    print('RECREATE_CHECK PASS — fresh clone + automatic deployment + reboot; no manual repairs',flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('RECREATE_CHECK FAILED: '+type(error).__name__+'; details suppressed',flush=True)
        raise SystemExit(1)
