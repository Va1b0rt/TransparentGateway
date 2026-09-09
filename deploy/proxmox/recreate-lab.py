#!/usr/bin/python3
"""One operator run: fresh lab clone, protected runtime delivery, deploy and reboot.

Explicitly limited to this lab: stop VM214, create VM216 from template9001.
Existing VMs and result directories are never overwritten. No automatic repair
or rollback: a failed run is preserved for diagnosis and cannot report PASS.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
PVE = 'root@192.168.88.2'
OLD = 'tgadmin@192.168.88.114'
NEW = 'tgadmin@192.168.88.116'
CLIENT = 'tgadmin@192.168.88.115'
SOURCE = '/var/lib/transparent-gateway/source'
SSH = ['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=5']


def run(argv, data=None, timeout=120):
    p = subprocess.run([str(x) for x in argv], input=data, capture_output=True, timeout=timeout)
    if p.returncode:
        if b'Operation not permitted' in p.stderr:
            raise RuntimeError('network sandbox denied access')
        raise RuntimeError('command failed; stdout and stderr suppressed')
    return p.stdout


def ssh(host, command, data=None, timeout=120):
    return run(SSH + [host, command], data, timeout)


def wait_for(check, timeout):
    deadline = time.monotonic() + timeout
    while True:
        try:
            result = check()
            if result:
                return result
        except (RuntimeError, subprocess.TimeoutExpired, ValueError, KeyError):
            pass
        if time.monotonic() >= deadline:
            raise TimeoutError('read-only readiness polling expired')
        time.sleep(5)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--evidence', type=Path, required=True)
    args = p.parse_args()
    assert os.geteuid() == 0 and args.evidence.is_absolute()
    os.umask(0o077)
    args.evidence.mkdir(mode=0o700)  # exclusive: never overwrite a previous run
    evidence = args.evidence
    commit = run(['git', '-C', ROOT, 'rev-parse', 'HEAD']).decode().strip()
    assert re.fullmatch('[0-9a-f]{40}', commit)
    assert not run(['git', '-C', ROOT, 'status', '--porcelain', '--untracked-files=no']).strip()
    state = {'status': 'running', 'commit': commit, 'template': 9001, 'vmid': 216,
             'hostname': 'tg-repro-05', 'gateway': NEW, 'client': CLIENT,
             'started_at': datetime.now(timezone.utc).isoformat(), 'steps': [],
             'manual_repairs': 0}
    started = time.monotonic()

    def save():
        tmp = evidence/'result.tmp'
        tmp.write_text(json.dumps(state, indent=2)+'\n')
        tmp.replace(evidence/'result.json')

    def step(name):
        state['current_step'] = name
        save()
        print('STEP '+name, flush=True)

    def passed(name, output=b''):
        if output:
            (evidence/(name+'.log')).write_bytes(output)
        state['steps'].append(name)
        save()
        print('PASS '+name, flush=True)

    try:
        step('preflight')
        resources = json.loads(ssh(PVE, 'pvesh get /cluster/resources --type vm --output-format json'))
        byid = {v['vmid']: v for v in resources}
        assert 216 not in byid
        assert byid[9001]['template'] == 1 and byid[9001]['status'] == 'stopped'
        assert byid[214]['name'] == 'tg-repro-04' and byid[214]['status'] == 'running'
        assert all(byid[i]['status'] == 'stopped' for i in (211, 212, 213))
        assert byid[215]['name'] == 'tg-client-01' and byid[215]['status'] == 'running'
        template = ssh(PVE, 'qm config 9001')
        (evidence/'template-before.txt').write_bytes(template)
        ssh(PVE, 'set -e; test ! -e /root/tg-rollout/recreate-216; test ! -e /var/lib/vz/tg-snippets/snippets/tg-216-user.yaml')
        assert ssh(OLD, 'hostname').strip() == b'tg-repro-04'
        state['old_machine_id'] = ssh(OLD, 'cat /etc/machine-id').decode().strip()
        state['old_boot_id'] = ssh(OLD, 'cat /proc/sys/kernel/random/boot_id').decode().strip()
        # Transfer only the six declared config files; never Redis, disks or images.
        code = "import sys;sys.path.insert(0,'/var/lib/transparent-gateway/source/deploy');import importlib.util;from pathlib import Path;s=importlib.util.spec_from_file_location('transfer','/var/lib/transparent-gateway/source/deploy/secret-transfer.py');m=importlib.util.module_from_spec(s);s.loader.exec_module(m);sys.stdout.buffer.write(m.document(Path('/etc/transparent-gateway/runtime')))"
        payload = ssh(OLD, 'sudo -n python3 -c '+shlex.quote(code))
        data = json.loads(payload)
        assert set(data) == {'.env','inventory/connectors.json','inventory/upstreams.jsonl',
                             'secrets/proxy-credentials.json','secrets/upstream-ca.crt','provider-secrets/api-token'}
        (evidence/'runtime-transfer.json').write_bytes(payload)
        state['runtime_files'] = len(data)
        bundle = evidence/'source.bundle'
        run(['git', '-C', ROOT, 'bundle', 'create', bundle, 'HEAD'])
        ssh(PVE, 'install -d -m 0700 /root/tg-rollout/recreate-216')
        ssh(PVE, 'cat > /root/tg-rollout/recreate-216/source.bundle', bundle.read_bytes())
        ssh(PVE, 'git clone /root/tg-rollout/recreate-216/source.bundle /root/tg-rollout/recreate-216/source')
        assert ssh(PVE, 'git -C /root/tg-rollout/recreate-216/source rev-parse HEAD').decode().strip() == commit
        passed('preflight')

        step('clean_clone')
        ssh(PVE, 'qm shutdown 214 --timeout 120', timeout=150)
        assert ssh(PVE, 'qm status 214').strip() == b'status: stopped'
        clone_start = time.monotonic()
        out = ssh(PVE, 'env VMID=216 VM_NAME=tg-repro-05 MGMT_CIDR=192.168.88.116/24 SSH_PUBLIC_KEYS=/root/tg-rollout/approved-keys.pub bash /root/tg-rollout/recreate-216/source/deploy/proxmox/clone-stage2.sh', timeout=600)
        state['clone_seconds'] = round(time.monotonic()-clone_start, 2)
        passed('clean_clone', out)

        step('cloud_init')
        def guest_key():
            result = json.loads(ssh(PVE, 'qm guest exec 216 -- /usr/bin/cat /etc/ssh/ssh_host_ed25519_key.pub', timeout=20))
            if result.get('exited') == 1 and result.get('exitcode') == 0:
                return result['out-data'].strip()
        key = wait_for(guest_key, 1200)
        assert re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]+)?', key)
        # Trust guest identity through the authenticated hypervisor, never keyscan/accept-new.
        known = Path('/root/.ssh/known_hosts')
        existing = subprocess.run(['ssh-keygen','-F','192.168.88.116','-f',str(known)],capture_output=True)
        if existing.returncode == 0:
            assert any(line.split()[1:3] == key.split()[:2] for line in existing.stdout.decode().splitlines() if not line.startswith('#'))
        else:
            with known.open('a') as f:
                f.write('192.168.88.116 '+key+'\n')
        out = ssh(NEW, 'sudo -n cloud-init status --wait --long', timeout=1200)
        assert b'extended_status: done' in out
        out += ssh(NEW, 'sudo -n /usr/local/sbin/tg-bootstrap --check')
        state['cloud_init_seconds'] = round(time.monotonic()-clone_start, 2)
        state['new_machine_id'] = ssh(NEW, 'cat /etc/machine-id').decode().strip()
        state['first_boot_id'] = ssh(NEW, 'cat /proc/sys/kernel/random/boot_id').decode().strip()
        assert state['new_machine_id'] != state['old_machine_id']
        clean = "import json,pathlib,subprocess;assert not pathlib.Path('/opt/transparent-gateway').exists();assert not pathlib.Path('/etc/transparent-gateway/runtime').exists();assert not pathlib.Path('/var/lib/transparent-gateway/source').exists();assert not subprocess.check_output(['docker','ps','-aq']).strip();assert not subprocess.check_output(['docker','volume','ls','-q']).strip();assert not subprocess.check_output(['docker','image','ls','-q']).strip();print('PASS clean application state: no checkout, runtime, containers, images or volumes')"
        out += ssh(NEW, 'sudo -n python3 -c '+shlex.quote(clean))
        state['clean_state_verified'] = True
        passed('cloud_init', out)

        step('secret_delivery')
        ssh(NEW, 'sudo -n sh -c '+shlex.quote('umask 077; cat > /var/lib/transparent-gateway/source.bundle'), bundle.read_bytes())
        ssh(NEW, 'sudo -n git clone /var/lib/transparent-gateway/source.bundle '+SOURCE)
        assert ssh(NEW, 'sudo -n git -C '+SOURCE+' rev-parse HEAD').decode().strip() == commit
        out = ssh(NEW, 'sudo -n python3 '+SOURCE+'/deploy/secret-transfer.py receive', payload)
        assert b'SECRET_DELIVERY PASS files=6' in out
        passed('secret_delivery', out)

        step('deployment')
        deploy_start = time.monotonic()
        out = ssh(NEW, 'sudo -n python3 '+SOURCE+'/deploy/deploy.py --source '+SOURCE+' --commit '+commit+' --lab-fixture --timeout 300', timeout=1800)
        state['deployment_seconds'] = round(time.monotonic()-deploy_start, 2)
        out += ssh(NEW, 'sudo -n python3 '+SOURCE+'/deploy/install-service.py --commit '+commit+' --lab-fixture', timeout=500)
        passed('deployment', out)

        step('first_client_readiness')
        out = run([sys.executable, ROOT/'deploy/verify-readiness.py','--public','--gateway',NEW,'--client',CLIENT], timeout=240)
        assert b'FULL_CLIENT_READINESS PASS' in out
        out += ssh(NEW, 'sudo -n python3 '+SOURCE+'/deploy/verify-secrets.py', timeout=180)
        assert b'SECRETS_CHECK PASS' in out
        state['first_ready_seconds'] = round(time.monotonic()-clone_start, 2)
        passed('first_client_readiness', out)

        step('automatic_reboot')
        out = ssh(NEW, 'sudo -n python3 '+SOURCE+'/deploy/verify-boot.py --prepare')
        ssh(PVE, 'qm reboot 216 --timeout 120', timeout=150)
        def ready_boot():
            out = ssh(NEW, 'cat /proc/sys/kernel/random/boot_id; systemctl is-active transparent-gateway.service', timeout=15).decode().splitlines()
            return out[0] if len(out) == 2 and out[0] != state['first_boot_id'] and out[1] == 'active' else None
        state['reboot_id'] = wait_for(ready_boot, 600)
        out += run([sys.executable,ROOT/'deploy/verify-full-boot.py','--gateway',NEW,'--client',CLIENT], timeout=360)
        assert b'FULL_REBOOT_CHECK PASS' in out
        out += ssh(NEW, 'sudo -n python3 '+SOURCE+'/deploy/verify-secrets.py', timeout=180)
        assert b'SECRETS_CHECK PASS' in out
        assert ssh(PVE, 'qm status 214').strip() == b'status: stopped'
        assert ssh(PVE, 'qm config 9001') == template
        (evidence/'new-vm-config.txt').write_bytes(ssh(PVE,'qm config 216'))
        state['template_unchanged'] = True
        state['old_vm_stopped'] = True
        passed('automatic_reboot', out)
        state['elapsed_seconds'] = round(time.monotonic()-started, 2)
        state['status'] = 'PASS'
        state['completed_at'] = datetime.now(timezone.utc).isoformat()
        save()
        print('RECREATE PASS — fresh clone, automatic deployment, client checks and reboot; no manual repairs', flush=True)
    except Exception as error:
        state['status'] = 'FAILED'
        state['error_type'] = type(error).__name__
        save()
        raise


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('RECREATE FAILED: '+type(error).__name__+'; preserve VM and protected evidence', flush=True)
        raise SystemExit(1)
