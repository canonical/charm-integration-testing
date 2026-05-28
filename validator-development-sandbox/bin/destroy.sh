#!/bin/bash
# Destroy the validator VM completely (irreversible).
#
# Usage:
#   validator-development-sandbox/bin/destroy.sh

set -euo pipefail

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
