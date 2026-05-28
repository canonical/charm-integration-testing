#!/bin/bash
# Open an interactive shell inside the validator VM.
#
# Usage:
#   validator-development-sandbox/bin/shell.sh

set -euo pipefail

VM_NAME="${VALIDATOR_VM:-validator-k8s}"
exec multipass exec "$VM_NAME" -- bash -l
