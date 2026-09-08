#!/usr/bin/env python3
"""Render identity-only cloud-config using the current users-list schema."""
import argparse
import json
import re
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("hostname")
parser.add_argument("public_keys", type=Path)
parser.add_argument("output", type=Path)
args = parser.parse_args()
if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", args.hostname):
    parser.error("hostname must be a simple lowercase DNS label")
keys = [line.strip() for line in args.public_keys.read_text().splitlines() if line.strip()]
if not keys or any(not line.startswith(("ssh-ed25519 ", "ssh-rsa ", "ecdsa-sha2-")) for line in keys):
    parser.error("provide approved SSH public keys, one per line")
config = "\n".join([
    "#cloud-config",
    "hostname: " + args.hostname,
    "manage_etc_hosts: true",
    "preserve_hostname: false",
    "ssh_pwauth: false",
    "disable_root: true",
    "package_update: false",
    "package_upgrade: false",
    "users:",
    "  - name: tgadmin",
    "    groups: [sudo]",
    "    shell: /bin/bash",
    "    lock_passwd: true",
    "    sudo: ['ALL=(ALL) NOPASSWD:ALL']",
    "    ssh_authorized_keys:",
    *["      - " + json.dumps(key) for key in keys],
    "",
])
# Refuse to change the input of an existing clone accidentally.
with args.output.open("x") as stream:
    stream.write(config)
args.output.chmod(0o600)
print("Wrote identity-only cloud-config:", args.output.name)
