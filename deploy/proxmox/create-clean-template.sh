#!/usr/bin/env bash
# Run as root on the selected Proxmox node. Never boots the template.
set -Eeuo pipefail
umask 022

TEMPLATE_ID=${TEMPLATE_ID:-9001}
STORAGE=${STORAGE:-local-lvm}
MGMT_BRIDGE=${MGMT_BRIDGE:-vmbr0}
CLIENT_BRIDGE=${CLIENT_BRIDGE:-vmbr91}
LAB_BRIDGE=${LAB_BRIDGE:-vmbr_tglab}
IMAGE_BUILD=20260831-2587
IMAGE=debian-13-genericcloud-amd64-${IMAGE_BUILD}.qcow2
IMAGE_BASE=https://cloud.debian.org/images/cloud/trixie/${IMAGE_BUILD}
CACHE=/var/lib/vz/template/iso/tg-${IMAGE_BUILD}

[[ $EUID == 0 ]] || { echo 'Run as root on Proxmox.' >&2; exit 1; }
command -v qm >/dev/null
[[ $TEMPLATE_ID =~ ^[0-9]+$ ]] || exit 1
if pvesh get /cluster/resources --type vm --output-format json |
    python3 -c 'import json,sys; sys.exit(not any(v["vmid"] == int(sys.argv[1]) for v in json.load(sys.stdin)))' "$TEMPLATE_ID"; then
    echo "VMID $TEMPLATE_ID already exists; refusing to overwrite." >&2
    exit 1
fi
for bridge in "$MGMT_BRIDGE" "$CLIENT_BRIDGE" "$LAB_BRIDGE"; do
    [[ -d /sys/class/net/$bridge/bridge ]] || { echo "Missing bridge: $bridge" >&2; exit 1; }
done
pvesm status --storage "$STORAGE"
mkdir -p "$CACHE"
cd "$CACHE"
curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
    "$IMAGE_BASE/SHA512SUMS" -o SHA512SUMS
awk -v name="$IMAGE" '$2 == name { print }' SHA512SUMS > image.sha512
[[ $(wc -l < image.sha512) == 1 ]] || { echo 'Missing image checksum.' >&2; exit 1; }
if [[ ! -f $IMAGE ]]; then
    curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
        "$IMAGE_BASE/$IMAGE" -o "$IMAGE.part"
    mv "$IMAGE.part" "$IMAGE"
fi
sha512sum --check image.sha512
qemu-img check "$IMAGE"

qm create "$TEMPLATE_ID" --name debian13-tg-clean-template \
    --ostype l26 --machine q35 --cpu host --cores 2 --memory 2048 \
    --scsihw virtio-scsi-single --serial0 socket --vga serial0 \
    --net0 "virtio,bridge=$MGMT_BRIDGE" \
    --net1 "virtio,bridge=$CLIENT_BRIDGE" \
    --net2 "virtio,bridge=$LAB_BRIDGE" \
    --ciupgrade 0 --onboot 0 \
    --tags 'transparent-gateway;clean-template' \
    --description "Unbooted Debian 13 genericcloud $IMAGE_BUILD; no application, data or secrets. net0=management; net1=client; net2=lab."
qm set "$TEMPLATE_ID" --scsi0 "$STORAGE:0,import-from=$CACHE/$IMAGE,discard=on"
qm set "$TEMPLATE_ID" --ide2 "$STORAGE:cloudinit" --boot order=scsi0
qm resize "$TEMPLATE_ID" scsi0 24G
qm template "$TEMPLATE_ID"
echo "PASS: template $TEMPLATE_ID created from verified image without booting."
