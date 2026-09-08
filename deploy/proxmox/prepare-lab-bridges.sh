#!/usr/bin/env bash
# Adds two isolated bridges. Does not reload or edit the management network.
set -Eeuo pipefail
[[ $EUID == 0 ]] || exit 1
command -v qm >/dev/null
target=/etc/network/interfaces.d/tg-repro
temporary=$(mktemp)
trap 'rm -f "$temporary"' EXIT
cat > "$temporary" <<'EOF'
# Transparent Gateway recreate lab: isolated, no physical ports or host addresses.
auto vmbr91
iface vmbr91 inet manual
    bridge-ports none
    bridge-stp off
    bridge-fd 0

auto vmbr_tglab
iface vmbr_tglab inet manual
    bridge-ports none
    bridge-stp off
    bridge-fd 0
EOF
if [[ -e $target ]]; then
    cmp --silent "$target" "$temporary" || {
        echo "Existing $target differs; inspect before changing it." >&2; exit 1;
    }
else
    for bridge in vmbr91 vmbr_tglab; do
        if ip link show "$bridge" >/dev/null 2>&1 || ifquery "$bridge" >/dev/null 2>&1; then
            echo "Bridge $bridge already exists outside this script." >&2; exit 1
        fi
    done
    install -m 0644 "$temporary" "$target"
fi
ifup vmbr91 vmbr_tglab
ip -brief link show type bridge
