#!/bin/bash
# Destroy the validator VM completely (irreversible).
#
# Usage:
#   validator-development-sandbox/bin/destroy.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEV_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Load optional .env for VALIDATOR_VM and other overrides.
if [ -f "$DEV_DIR/.env" ]; then
    set -a
    # shellcheck source=/dev/null
    source "$DEV_DIR/.env"
    set +a
fi

VM_NAME="${VALIDATOR_VM:-validator-k8s}"

echo "This will permanently delete VM '$VM_NAME' and all its data."
read -r -p "Type the VM name to confirm: " confirm

if [ "$confirm" != "$VM_NAME" ]; then
    echo "Aborted."
    exit 1
fi

echo "==> Deleting $VM_NAME..."
multipass delete "$VM_NAME"
multipass purge
echo "==> Done."
