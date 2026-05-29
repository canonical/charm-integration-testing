#!/bin/bash
# Stop the validator VM (preserves all state).
#
# Usage:
#   validator-development-sandbox/bin/down.sh

set -euo pipefail

VM_NAME="${VALIDATOR_VM:-validator-k8s}"
echo "==> Stopping $VM_NAME..."
multipass stop "$VM_NAME"
echo "==> Done. Run 'validator-development-sandbox/bin/up.sh' to resume."
