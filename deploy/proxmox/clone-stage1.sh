#!/usr/bin/env bash
# Stage 1: identity and network only; package/bootstrap installation follows later.
set -Eeuo pipefail
TEMPLATE_ID=${TEMPLATE_ID:-9001}
VMID=${VMID:-211}
VM_NAME=${VM_NAME:-tg-repro-01}
STORAGE=${STORAGE:-local-lvm}
SNIPPET_STORAGE=${SNIPPET_STORAGE:-tg-snippets}
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
USER_DATA_RENDERER=${USER_DATA_RENDERER:-$SCRIPT_DIR/render-stage1-user-data.py}
BOOTSTRAP_STAGE=${BOOTSTRAP_STAGE:-1}
GUEST_AGENT=${GUEST_AGENT:-0}
SSH_PUBLIC_KEYS=${SSH_PUBLIC_KEYS:?Set SSH_PUBLIC_KEYS to a file of approved public keys}
MGMT_CIDR=${MGMT_CIDR:-192.168.88.114/24}
MGMT_GATEWAY=${MGMT_GATEWAY:-192.168.88.1}
CLIENT_CIDR=${CLIENT_CIDR:-192.168.91.1/24}
LAB_CIDR=${LAB_CIDR:-10.10.20.2/24}

[[ $EUID == 0 ]] || exit 1
[[ -s $SSH_PUBLIC_KEYS ]] || exit 1
[[ $VMID =~ ^[0-9]+$ ]] || exit 1
[[ $VMID != "$TEMPLATE_ID" ]] || exit 1
qm config "$TEMPLATE_ID" | grep -qx 'template: 1'
if pvesh get /cluster/resources --type vm --output-format json |
    python3 -c 'import json,sys; sys.exit(not any(v["vmid"] == int(sys.argv[1]) for v in json.load(sys.stdin)))' "$VMID"; then
    echo "VMID $VMID already exists; refusing to overwrite." >&2
    exit 1
fi
snippet_volume="$SNIPPET_STORAGE:snippets/tg-$VMID-user.yaml"
snippet_path=$(pvesm path "$snippet_volume")
MGMT_CIDR="$MGMT_CIDR" MGMT_GATEWAY="$MGMT_GATEWAY" CLIENT_CIDR="$CLIENT_CIDR" LAB_CIDR="$LAB_CIDR" \
    python3 "$USER_DATA_RENDERER" "$VM_NAME" "$SSH_PUBLIC_KEYS" "$snippet_path"
# qm clone also rejects a VMID already allocated anywhere in the cluster.
qm clone "$TEMPLATE_ID" "$VMID" --name "$VM_NAME" --full 1 --storage "$STORAGE"
qm set "$VMID" --cicustom "user=$snippet_volume" \
    --agent "enabled=$GUEST_AGENT" \
    --ipconfig0 "ip=$MGMT_CIDR,gw=$MGMT_GATEWAY" \
    --ipconfig1 "ip=$CLIENT_CIDR" --ipconfig2 "ip=$LAB_CIDR" \
    --nameserver "$MGMT_GATEWAY" --searchdomain home.lab \
    --ciupgrade 0 --onboot 0 --tags 'transparent-gateway;repro-lab' \
    --description "Stage $BOOTSTRAP_STAGE clone. net0/eth0=management $MGMT_CIDR; net1/eth1=client $CLIENT_CIDR; net2/eth2=lab $LAB_CIDR."
qm cloudinit update "$VMID"
# Proxmox generates per-clone MAC bindings; do not copy another VM's network YAML.
qm cloudinit dump "$VMID" network
qm start "$VMID"
echo "Started $VM_NAME ($VMID). Connect: ssh tgadmin@${MGMT_CIDR%/*}"
