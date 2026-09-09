#!/usr/bin/python3
"""Operator check: automatic systemd boot plus fresh client DNS/local/public readiness."""
import argparse
from pathlib import Path
import re
import subprocess
import sys


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gateway',default='tgadmin@192.168.88.114')
    parser.add_argument('--client',default='tgadmin@192.168.88.115')
    args=parser.parse_args()
    assert re.fullmatch(r'[a-z_][a-z0-9_-]*@[a-zA-Z0-9.-]+',args.gateway)
    result=subprocess.run(['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=5',args.gateway,
        'sudo -n python3 /var/lib/transparent-gateway/source/deploy/verify-boot.py'],capture_output=True,text=True,timeout=120)
    if result.returncode or 'REBOOT_CHECK PASS' not in result.stdout:
        raise RuntimeError('boot evidence failed; details suppressed')
    print(result.stdout.strip(),flush=True)
    result=subprocess.run([sys.executable,str(Path(__file__).with_name('verify-readiness.py')),
        '--public','--gateway',args.gateway,'--client',args.client],capture_output=True,text=True,timeout=180)
    if result.returncode or 'FULL_CLIENT_READINESS PASS' not in result.stdout:
        raise RuntimeError('client readiness failed; details suppressed')
    print(result.stdout.strip(),flush=True)
    print('FULL_REBOOT_CHECK PASS — automatic startup and fresh client DNS/TCP/local/public echo')


if __name__=='__main__':
    try:main()
    except Exception as error:
        print('FULL_REBOOT_CHECK FAILED: '+type(error).__name__+'; inspect private operator evidence')
        raise SystemExit(1)
