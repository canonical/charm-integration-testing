#!/bin/bash
# Provision or resume the validator development VM.
#
# Usage:
#   validator-development-sandbox/bin/up.sh
#
# Environment:
#   VALIDATOR_VM   VM name override (default: validator-k8s)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEV_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_DIR="$(cd "$DEV_DIR/.." && pwd)"
VM_NAME="${VALIDATOR_VM:-validator-k8s}"

_vm_state() {
    multipass info "$VM_NAME" --format json 2>/dev/null \
        | python3 -c "import sys,json; print(json.load(sys.stdin)['info']['$VM_NAME']['state'])" 2>/dev/null \
        || echo "absent"
}

echo "==> Checking VM state..."
state=$(_vm_state)

case "$state" in
    absent)
        echo "==> Launching $VM_NAME (k8s + juju bootstrap)..."
        multipass launch 24.04 \
            --name "$VM_NAME" \
            --cpus 4 \
            --memory 8G \
            --disk 40G \
            --cloud-init "$DEV_DIR/validator-substrate.yaml" \
            --timeout 1800
        ;;
    Stopped)
        echo "==> Starting $VM_NAME..."
        multipass start "$VM_NAME"
        ;;
    Running)
        echo "==> $VM_NAME already running."
        ;;
    *)
        echo "==> VM is in state '$state', attempting to start..."
        multipass start "$VM_NAME"
        ;;
esac

# Mount project if not already mounted
echo "==> Checking project mount..."
if ! multipass info "$VM_NAME" | grep -qF "$PROJECT_DIR"; then
    echo "==> Mounting $PROJECT_DIR -> /project..."
    multipass mount "$PROJECT_DIR" "$VM_NAME:/project"
fi

# Set up Python venv with project packages (poetry manages the venv)
echo "==> Installing Python dependencies via poetry..."
multipass exec "$VM_NAME" -- bash -lc "
    set -euo pipefail
    # Install pipx and poetry if not present
    if ! command -v poetry &>/dev/null && ! test -f ~/.local/bin/poetry; then
        if ! command -v pipx &>/dev/null; then
            sudo apt-get install -y -q pipx
        fi
        pipx install 'poetry==2.*'
        pipx ensurepath
        export PATH="\$HOME/.local/bin:\$PATH"
    fi
    cd /project
    poetry install
"

# Install node-based tools used by the sandbox if not present
echo "==> Checking for node-based tools..."
multipass exec "$VM_NAME" -- bash -lc "
    set -euo pipefail
    if ! command -v node &>/dev/null; then
        sudo snap install node --classic
    fi

    if ! command -v copilot &>/dev/null; then
        echo '==> Installing @github/copilot...'
        sudo npm install -g @github/copilot
    else
        echo '==> @github/copilot: found.'
    fi

    echo '==> Linking project skills to ~/.agents/skills...'
    mkdir -p ~/.agents
    ln -sfn /project/validator-development-sandbox/prompts ~/.agents/skills

    if ! command -v markdownlint-cli2 &>/dev/null; then
        echo '==> Installing markdownlint-cli2...'
        sudo npm install -g markdownlint-cli2
    else
        echo '==> markdownlint-cli2: found.'
    fi

    if ! command -v gh &>/dev/null; then
        echo '==> Installing gh (GitHub CLI)...'
        sudo snap install gh --classic
    else
        echo '==> gh: found.'
    fi
"

# Verify GitHub auth is available for copilot (token injected at runtime by bin/run.sh).
echo "==> Checking GitHub auth..."
if gh auth token &>/dev/null; then
    echo "==> GitHub token available — copilot will authenticate via GH_TOKEN."
else
    echo "==> Warning: 'gh auth token' returned nothing."
    echo "==>   Run: gh auth login   on this machine, then re-run bin/up.sh."
fi

echo ""
echo "Substrate ready."
echo "  Run agent    : validator-development-sandbox/bin/run.sh 'your task here'"
echo "  Interactive  : validator-development-sandbox/bin/run.sh --interactive"
echo "  Shell        : validator-development-sandbox/bin/shell.sh"
