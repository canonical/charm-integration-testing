#!/bin/bash
# Stop the validator VM (preserves all state).
#
# Usage:
#   validator-development-sandbox/bin/down.sh

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
echo "==> Stopping $VM_NAME..."
multipass stop "$VM_NAME"
echo "==> Done. Run 'validator-development-sandbox/bin/up.sh' to resume."
