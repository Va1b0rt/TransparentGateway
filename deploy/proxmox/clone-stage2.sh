#!/usr/bin/env bash
# Fresh first-boot test of packages and bootstrap, using the unbooted template.
set -Eeuo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
export VMID=${VMID:-213}
export VM_NAME=${VM_NAME:-tg-repro-03}
export USER_DATA_RENDERER=$SCRIPT_DIR/render-stage2-user-data.py
export BOOTSTRAP_STAGE=2
export GUEST_AGENT=1
exec bash "$SCRIPT_DIR/clone-stage1.sh"
