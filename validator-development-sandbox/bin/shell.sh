#!/bin/bash
# Open an interactive shell inside the validator VM.
#
# Usage:
#   validator-development-sandbox/bin/shell.sh

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
exec multipass exec "$VM_NAME" -- bash -lc "cd /project && exec bash -l"
