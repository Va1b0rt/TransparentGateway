#!/usr/bin/env bash
# Isolated E2E client: management SSH, but sole default route through lab gateway.
set -Eeuo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
SSH_PUBLIC_KEYS=${SSH_PUBLIC_KEYS:?approved public key file required}
VMID=${VMID:-215}
VM_NAME=${VM_NAME:-tg-client-01}
[[ $VMID =~ ^[0-9]+$ && $VMID != 9001 && $EUID == 0 ]]
if pvesh get /cluster/resources --type vm --output-format json | python3 -c 'import json,sys; sys.exit(not any(v["vmid"]==int(sys.argv[1]) for v in json.load(sys.stdin)))' "$VMID"; then
    echo 'VMID already exists; refusing overwrite' >&2; exit 1
fi
snippet="tg-snippets:snippets/tg-$VMID-user.yaml"
python3 "$SCRIPT_DIR/render-stage1-user-data.py" "$VM_NAME" "$SSH_PUBLIC_KEYS" "$(pvesm path "$snippet")"
qm clone 9001 "$VMID" --name "$VM_NAME" --full 1 --storage local-lvm
qm set "$VMID" --cores 1 --memory 768 --delete net2 --agent enabled=0 \
 --cicustom "user=$snippet" --ipconfig0 ip=192.168.88.115/24 \
 --ipconfig1 ip=192.168.91.2/24,gw=192.168.91.1 --nameserver 192.168.91.1 \
 --ciupgrade 0 --onboot 0 --tags 'transparent-gateway;client-lab' \
 --description 'E2E client: eth0 management192.168.88.115 without default; eth1 client192.168.91.2 with sole default via192.168.91.1. No application or secrets.'
qm cloudinit update "$VMID"
qm start "$VMID"
echo 'Client started: tgadmin@192.168.88.115; client192.168.91.2 -> gateway192.168.91.1'
