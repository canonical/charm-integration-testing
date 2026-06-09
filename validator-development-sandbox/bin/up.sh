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

# Load optional .env for VALIDATOR_VM and other overrides.
if [ -f "$DEV_DIR/.env" ]; then
    set -a
    # shellcheck source=/dev/null
    source "$DEV_DIR/.env"
    set +a
fi

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
if ! multipass info "$VM_NAME" | grep -qF "/project"; then
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

# Configure git in VM: propagate host identity settings and install a
# dedicated VM signing key (no passphrase, stored in .env as base64 so it
# survives VM recreation). The public key must be added to GitHub once.
echo "==> Configuring git in VM..."

# Generate and persist a VM-specific signing key in .env if not present.
_ENV_FILE="$DEV_DIR/.env"
if ! grep -q '^VALIDATOR_SIGNING_KEY=' "$_ENV_FILE" 2>/dev/null; then
    echo "==> Generating VM signing key (add the public key to GitHub once)..."
    _tmp_key=$(mktemp)
    rm -f "$_tmp_key"  # ssh-keygen won't overwrite an existing file without prompting
    ssh-keygen -t ed25519 -C "validator-vm-signing" -N "" -f "$_tmp_key" -q
    _key_b64=$(base64 -w0 < "$_tmp_key")
    _key_pub=$(cat "${_tmp_key}.pub")
    rm -f "$_tmp_key" "${_tmp_key}.pub"
    touch "$_ENV_FILE"
    echo "VALIDATOR_SIGNING_KEY=$_key_b64" >> "$_ENV_FILE"
    echo ""
    echo "  *** Add this signing key to your GitHub account: ***"
    echo "  https://github.com/settings/ssh"
    echo "  Key type: Signing Key"
    echo "  $_key_pub"
    echo ""
fi
_VM_SIGNING_KEY_B64=$(grep '^VALIDATOR_SIGNING_KEY=' "$_ENV_FILE" | cut -d= -f2-)

# Install the signing key into the VM.
multipass exec "$VM_NAME" -- install -d -m 700 /home/ubuntu/.ssh
echo "$_VM_SIGNING_KEY_B64" | base64 -d \
    | multipass exec "$VM_NAME" -- tee /home/ubuntu/.ssh/id_signing > /dev/null
multipass exec "$VM_NAME" -- chmod 600 /home/ubuntu/.ssh/id_signing

# Build the gitconfig: propagate host identity (name, email, preferences) from
# 'git config --list', strip signing config, then append VM-specific signing.
{
    git -C "$PROJECT_DIR" config --global --list \
        | grep -v '^includeif\.' \
        | grep -v '^\(user\.signingkey\|gpg\.\|commit\.gpgsign\|tag\.gpgsign\)=' \
        | awk '
        {
            eq = index($0, "=")
            full_key = substr($0, 1, eq - 1)
            value = substr($0, eq + 1)
            dot1 = index(full_key, ".")
            section = substr(full_key, 1, dot1 - 1)
            rest = substr(full_key, dot1 + 1)
            dot_last = 0
            for (i = length(rest); i >= 1; i--) {
                if (substr(rest, i, 1) == ".") { dot_last = i; break }
            }
            if (dot_last > 0) {
                header = "[" section " \"" substr(rest, 1, dot_last - 1) "\"]"
                setting = substr(rest, dot_last + 1)
            } else {
                header = "[" section "]"
                setting = rest
            }
            if (header != prev) { print header; prev = header }
            print "\t" setting " = " value
        }'
    # Append VM signing config pointing to the private key file.
    printf '[user]\n\tsigningkey = /home/ubuntu/.ssh/id_signing\n[gpg]\n\tformat = ssh\n[commit]\n\tgpgsign = true\n[tag]\n\tgpgsign = true\n'
} | multipass exec "$VM_NAME" -- tee /home/ubuntu/.gitconfig > /dev/null

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
