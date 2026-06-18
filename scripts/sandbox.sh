#!/bin/bash
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
# Host-side entry point for the charm-integration-testing development sandbox.
# Usage:
#   scripts/sandbox.sh up            Create or resume the VM and install deps
#   scripts/sandbox.sh down          Stop the VM (preserves state)
#   scripts/sandbox.sh destroy       Delete the VM permanently
#   scripts/sandbox.sh shell         Open a shell inside the VM
#   scripts/sandbox.sh run 'task'    Run Copilot in autonomous mode
#   scripts/sandbox.sh run --interactive   Run Copilot in interactive mode
#   scripts/sandbox.sh --help        Show this help
#
# Environment / .env (optional, loaded from development-sandbox/.env):
#   GITHUB_TOKEN          Fine-grained PAT for gh CLI inside the VM
#   COPILOT_GITHUB_TOKEN  Override for Copilot AI auth (default: gh auth token)
#   SANDBOX_VM            VM name override (default: charm-qa-sandbox)
#   COPILOT_MODEL         Copilot model override (default: sonnet-4.6)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DEV_DIR="$PROJECT_DIR/development-sandbox"

# Capture host gh token before .env is sourced so GITHUB_TOKEN in .env does
# not interfere with the Copilot auth token lookup.
_gh_token=$(gh auth token 2>/dev/null || true)

# ---------------------------------------------------------------------------
# Load .env
# ---------------------------------------------------------------------------
if [ -f "$DEV_DIR/.env" ]; then
    set -a
    # shellcheck source=/dev/null
    source "$DEV_DIR/.env"
    set +a
fi

VM_NAME="${SANDBOX_VM:-charm-qa-sandbox}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_vm_state() {
    multipass info "$VM_NAME" --format json 2>/dev/null \
        | python3 -c "import sys,json; print(json.load(sys.stdin)['info']['$VM_NAME']['state'])" 2>/dev/null \
        || echo "absent"
}

_usage() {
    cat <<'EOF'
Usage:
  scripts/sandbox.sh up                        Create or resume the VM
  scripts/sandbox.sh down                      Stop the VM (preserves state)
  scripts/sandbox.sh destroy                   Delete the VM permanently
  scripts/sandbox.sh shell                     Open a shell inside the VM
  scripts/sandbox.sh run 'task description'    Autonomous Copilot mode
  scripts/sandbox.sh run --interactive         Interactive Copilot mode
  scripts/sandbox.sh --help                    Show this help

Environment (.env keys):
  SANDBOX_VM             VM name override (default: charm-qa-sandbox)
  GITHUB_TOKEN           Fine-grained PAT for gh CLI inside the VM
  COPILOT_GITHUB_TOKEN   Copilot AI auth token (default: gh auth token)
  COPILOT_MODEL          Copilot model (default: sonnet-4.6)

Inside an interactive session use skill slash commands:
  /develop-validator     Develop a new charm integration validator
  /test-validator        Test an existing validator
  /setup-k8s             Set up Canonical k8s substrate
  /setup-lxd             Set up LXD substrate
EOF
}

# ---------------------------------------------------------------------------
# Subcommand: up
# ---------------------------------------------------------------------------
_cmd_up() {
    echo "==> Checking VM state..."
    state=$(_vm_state)

    case "$state" in
        absent)
            echo "==> Launching $VM_NAME..."
            multipass launch 24.04 \
                --name "$VM_NAME" \
                --cpus 4 \
                --memory 8G \
                --disk 40G \
                --cloud-init "$DEV_DIR/substrate.yaml" \
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
        if ! command -v poetry &>/dev/null && ! test -f ~/.local/bin/poetry; then
            if ! command -v pipx &>/dev/null; then
                sudo apt-get install -y -q pipx
            fi
            pipx install 'poetry==2.*'
            pipx ensurepath
            export PATH=\"\$HOME/.local/bin:\$PATH\"
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
        ln -sfn /project/development-sandbox/prompts ~/.agents/skills

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

    # Trust all folders in Copilot so it never prompts for folder confirmation.
    echo "==> Configuring Copilot trusted folders..."
    multipass exec "$VM_NAME" -- bash -lc "
        mkdir -p ~/.copilot
        cat > ~/.copilot/config.json <<'JSON'
{
  \"appTipShown\": true,
  \"trustedFolders\": [
    \"/\"
  ]
}
JSON
    "

    # Configure git in VM: propagate host identity and install a VM-specific
    # signing key (no passphrase, stored in .env as base64 so it survives
    # VM recreation). The public key must be added to GitHub once as a Signing Key.
    echo "==> Configuring git in VM..."

    _ENV_FILE="$DEV_DIR/.env"
    if ! grep -q '^SANDBOX_SIGNING_KEY=' "$_ENV_FILE" 2>/dev/null; then
        echo "==> Generating VM signing key..."
        _tmp_key=$(mktemp)
        rm -f "$_tmp_key"
        ssh-keygen -t ed25519 -C "sandbox-vm-signing" -N "" -f "$_tmp_key" -q
        _key_b64=$(base64 -w0 < "$_tmp_key")
        rm -f "$_tmp_key" "${_tmp_key}.pub"
        touch "$_ENV_FILE"
        chmod 600 "$_ENV_FILE"
        echo "SANDBOX_SIGNING_KEY=$_key_b64" >> "$_ENV_FILE"
    fi
    _VM_SIGNING_KEY_B64=$(grep '^SANDBOX_SIGNING_KEY=' "$_ENV_FILE" | cut -d= -f2-)

    # Install the signing key into the VM and generate the .pub file.
    multipass exec "$VM_NAME" -- install -d -m 700 /home/ubuntu/.ssh
    echo "$_VM_SIGNING_KEY_B64" | base64 -d \
        | multipass exec "$VM_NAME" -- tee /home/ubuntu/.ssh/id_signing > /dev/null
    multipass exec "$VM_NAME" -- chmod 600 /home/ubuntu/.ssh/id_signing
    multipass exec "$VM_NAME" -- bash -c \
        "ssh-keygen -y -f /home/ubuntu/.ssh/id_signing > /home/ubuntu/.ssh/id_signing.pub"

    # Build the gitconfig: propagate host identity, strip signing config,
    # then append VM-specific signing settings.
    {
        git -C "$PROJECT_DIR" config --list \
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
        printf '[user]\n\tsigningkey = /home/ubuntu/.ssh/id_signing\n[gpg]\n\tformat = ssh\n[commit]\n\tgpgsign = true\n[tag]\n\tgpgsign = true\n'
    } | multipass exec "$VM_NAME" -- tee /home/ubuntu/.gitconfig > /dev/null

    # Print the signing public key so the user can add it to GitHub.
    _signing_pub=$(multipass exec "$VM_NAME" -- cat /home/ubuntu/.ssh/id_signing.pub)
    echo ""
    echo "  *** VM signing key (add to GitHub if not already done) ***"
    echo "  https://github.com/settings/ssh  ->  New SSH key  ->  Key type: Signing Key"
    echo "  $_signing_pub"
    echo ""

    # Check GitHub auth
    echo "==> Checking GitHub auth..."
    if gh auth token &>/dev/null; then
        echo "==> GitHub token available — Copilot will authenticate via GH_TOKEN."
    else
        echo "==> Warning: 'gh auth token' returned nothing."
        echo "==>   Run: gh auth login   on this machine, then re-run scripts/sandbox.sh up."
    fi

    echo ""
    echo "Sandbox ready."
    echo "  Run agent    : scripts/sandbox.sh run 'your task here'"
    echo "  Interactive  : scripts/sandbox.sh run --interactive"
    echo "  Shell        : scripts/sandbox.sh shell"
}

# ---------------------------------------------------------------------------
# Subcommand: down
# ---------------------------------------------------------------------------
_cmd_down() {
    echo "==> Stopping $VM_NAME..."
    multipass stop "$VM_NAME"
    echo "==> Done. Run 'scripts/sandbox.sh up' to resume."
}

# ---------------------------------------------------------------------------
# Subcommand: destroy
# ---------------------------------------------------------------------------
_cmd_destroy() {
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
}

# ---------------------------------------------------------------------------
# Subcommand: shell
# ---------------------------------------------------------------------------
_cmd_shell() {
    exec multipass exec "$VM_NAME" -- bash -lc "cd /project && exec bash -l"
}

# ---------------------------------------------------------------------------
# Subcommand: run
# ---------------------------------------------------------------------------
_cmd_run() {
    COPILOT_MODEL="${COPILOT_MODEL:-sonnet-4.6}"
    _copilot_token="${COPILOT_GITHUB_TOKEN:-$_gh_token}"

    INTERACTIVE=false
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --interactive)
                INTERACTIVE=true
                shift
                ;;
            -h|--help)
                cat <<'EOF'
Usage:
  scripts/sandbox.sh run 'task description'   autonomous mode
  scripts/sandbox.sh run --interactive        interactive mode
EOF
                exit 0
                ;;
            --)
                shift
                break
                ;;
            -*)
                echo "Unknown option: $1" >&2
                echo "Run scripts/sandbox.sh --help for usage." >&2
                exit 1
                ;;
            *)
                break
                ;;
        esac
    done

    TASK="${*:-}"

    PROMPT_FILE=$(multipass exec "$VM_NAME" -- bash -c "mktemp /tmp/copilot-prompt-XXXXXX")

    {
        cat "$DEV_DIR/prompts/system.md"
        if [ -n "$TASK" ]; then
            printf "\n\n---\n\nTask: %s\n" "$TASK"
        fi
    } | multipass exec "$VM_NAME" -- bash -c "cat > $PROMPT_FILE"

    if [ "$INTERACTIVE" = "true" ]; then
        CONTEXT_MSG="Please read $PROMPT_FILE for project context, then await my instructions."
        # Build env var array, only including GH_TOKEN/GITHUB_TOKEN if they are actually set
        _env_args=()
        [ -n "${GITHUB_TOKEN:-}" ] && _env_args+=("GH_TOKEN=$GITHUB_TOKEN" "GITHUB_TOKEN=$GITHUB_TOKEN")
        _env_args+=("COPILOT_GITHUB_TOKEN=$_copilot_token" "COPILOT_MODEL=$COPILOT_MODEL")
        multipass exec "$VM_NAME" -- env "${_env_args[@]}" bash -lc "
            cd /project
            copilot --yolo -i \"$CONTEXT_MSG\"
            rm -f $PROMPT_FILE
        "
    else
        [ -n "$TASK" ] || { echo "Usage: scripts/sandbox.sh run 'task description'"; exit 1; }
        # Build env var array, only including GH_TOKEN/GITHUB_TOKEN if they are actually set
        _env_args=()
        [ -n "${GITHUB_TOKEN:-}" ] && _env_args+=("GH_TOKEN=$GITHUB_TOKEN" "GITHUB_TOKEN=$GITHUB_TOKEN")
        _env_args+=("COPILOT_GITHUB_TOKEN=$_copilot_token" "COPILOT_MODEL=$COPILOT_MODEL")
        multipass exec "$VM_NAME" -- env "${_env_args[@]}" bash -lc "
            cd /project
            copilot --yolo -p \"\$(cat $PROMPT_FILE)\"
            rm -f $PROMPT_FILE
        "
    fi
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
case "${1:-}" in
    up)
        _cmd_up
        ;;
    down)
        _cmd_down
        ;;
    destroy)
        _cmd_destroy
        ;;
    shell)
        _cmd_shell
        ;;
    run)
        shift
        _cmd_run "$@"
        ;;
    --help|-h|help)
        _usage
        ;;
    "")
        _usage
        exit 1
        ;;
    *)
        echo "Unknown subcommand: $1" >&2
        echo "Run scripts/sandbox.sh --help for usage." >&2
        exit 1
        ;;
esac
