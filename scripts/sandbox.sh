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
#   SANDBOX_MOUNT         VM-side mount path (default: /project)
#   COPILOT_MODEL         Copilot model override (default: sonnet-4.6)
#   CHARMHUB_API_URL      Override for bundle_builder_x's Charmhub API base URL
#   SNAPCRAFT_API_URL     Override for bundle_builder_x's Snapcraft API base URL

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
VM_MOUNT="${SANDBOX_MOUNT:-/project}"
VM_CPUS="${SANDBOX_CPUS:-4}"
VM_MEMORY="${SANDBOX_MEMORY:-8G}"
VM_DISK="${SANDBOX_DISK:-40G}"
[[ "$VM_MOUNT" = /* ]] || { echo "ERROR: SANDBOX_MOUNT must be an absolute path: $VM_MOUNT" >&2; exit 1; }
[[ "$VM_MOUNT" != *"'"* ]] || { echo "ERROR: SANDBOX_MOUNT must not contain single quotes: $VM_MOUNT" >&2; exit 1; }
[[ "$VM_MOUNT" != *":"* ]] || { echo "ERROR: SANDBOX_MOUNT must not contain colons: $VM_MOUNT" >&2; exit 1; }
[[ "$VM_NAME" != *"'"* ]] || { echo "ERROR: SANDBOX_VM must not contain single quotes: $VM_NAME" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_vm_state() {
    multipass info "$VM_NAME" --format json 2>/dev/null \
        | python3 -c "import sys,json; print(json.load(sys.stdin)['info']['$VM_NAME']['state'])" 2>/dev/null \
        || echo "absent"
}

_is_mounted() {
    multipass info "$VM_NAME" --format json 2>/dev/null \
        | python3 -c "import sys,json; mounts=json.load(sys.stdin)['info'][sys.argv[1]].get('mounts',{}); exit(0 if sys.argv[2] in mounts else 1)" "$VM_NAME" "$VM_MOUNT" 2>/dev/null
}

_usage() {
    cat <<'EOF'
Usage:
  scripts/sandbox.sh up [--cpus N] [--memory SIZE] [--disk SIZE]
                                               Create or resume the VM
  scripts/sandbox.sh down                      Stop the VM (preserves state)
  scripts/sandbox.sh destroy                   Delete the VM permanently
  scripts/sandbox.sh shell                     Open a shell inside the VM
  scripts/sandbox.sh run 'task description'    Autonomous Copilot mode
  scripts/sandbox.sh run --interactive         Interactive Copilot mode
  scripts/sandbox.sh --help                    Show this help

Environment (.env keys):
  SANDBOX_VM             VM name override (default: charm-qa-sandbox)
  SANDBOX_MOUNT          VM-side mount path (default: /project)
  SANDBOX_CPUS           vCPU count for new VMs (default: 4)
  SANDBOX_MEMORY         RAM for new VMs, e.g. 8G or 16G (default: 8G)
  SANDBOX_DISK           Disk size for new VMs, e.g. 40G or 80G (default: 40G)
  GITHUB_TOKEN           Fine-grained PAT for gh CLI inside the VM
  COPILOT_GITHUB_TOKEN   Copilot AI auth token (default: gh auth token)
  COPILOT_MODEL          Copilot model (default: sonnet-4.6)
  CHARMHUB_API_URL       Override for bundle_builder_x's Charmhub API base URL
  SNAPCRAFT_API_URL      Override for bundle_builder_x's Snapcraft API base URL
  SANDBOX_MCP_CONFIG_FILE  Path to an MCP server config JSON file on the host

Inside an interactive session use skill slash commands:
  /develop-validator     Develop a new charm integration validator
  /test-validator        Test an existing validator
  /review-pr             Review and address pull request feedback
  /setup-k8s             Set up Canonical k8s substrate
  /setup-lxd             Set up LXD substrate

VM resource flags (only applied when the VM does not yet exist):
  --cpus N               vCPU count (default: SANDBOX_CPUS or 4)
  --memory SIZE          RAM, e.g. 16G (default: SANDBOX_MEMORY or 8G)
  --disk SIZE            Disk, e.g. 80G (default: SANDBOX_DISK or 40G)
EOF
}

# ---------------------------------------------------------------------------
# Subcommand: up
# ---------------------------------------------------------------------------
_cmd_up() {
    # Parse optional resource overrides; CLI flags take precedence over .env / defaults.
    local cpus="$VM_CPUS" memory="$VM_MEMORY" disk="$VM_DISK"
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --cpus)
                if [ $# -lt 2 ] || [ -z "${2:-}" ]; then
                    echo "Error: --cpus requires a value" >&2
                    exit 1
                fi
                cpus="$2"
                shift 2
                ;;
            --memory)
                if [ $# -lt 2 ] || [ -z "${2:-}" ]; then
                    echo "Error: --memory requires a value" >&2
                    exit 1
                fi
                memory="$2"
                shift 2
                ;;
            --disk)
                if [ $# -lt 2 ] || [ -z "${2:-}" ]; then
                    echo "Error: --disk requires a value" >&2
                    exit 1
                fi
                disk="$2"
                shift 2
                ;;
            --)        shift; break ;;
            -*)
                echo "Unknown option: $1" >&2
                echo "Run scripts/sandbox.sh --help for usage." >&2
                exit 1
                ;;
            *) break ;;
        esac
    done

    echo "==> Checking VM state..."
    state=$(_vm_state)

    case "$state" in
        absent)
            echo "==> Launching $VM_NAME (cpus=$cpus memory=$memory disk=$disk)..."
            multipass launch 24.04 \
                --name "$VM_NAME" \
                --cpus "$cpus" \
                --memory "$memory" \
                --disk "$disk" \
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
    if ! _is_mounted; then
        echo "==> Mounting $PROJECT_DIR -> $VM_MOUNT..."
        multipass exec "$VM_NAME" -- bash -c "sudo mkdir -p '$VM_MOUNT' && sudo chown ubuntu:ubuntu '$VM_MOUNT'"
        multipass mount "$PROJECT_DIR" "$VM_NAME:$VM_MOUNT"
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
        cd '$VM_MOUNT'
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

        # Offer to register the signing key with GitHub via the API instead
        # of requiring the user to paste it in manually.
        if gh api user/ssh_signing_keys --jq '.[].key' 2>/dev/null | grep -qF "$(awk '{print $1, $2}' <<< "$_signing_pub")"; then
            echo "==> Signing key already registered with GitHub."
        elif [ -t 0 ]; then
            read -r -p "  Register this signing key with GitHub now via the API? [y/N] " _register_key
            if [[ "$_register_key" =~ ^[Yy]$ ]]; then
                if gh api user/ssh_signing_keys -f "title=sandbox-vm-signing ($VM_NAME)" -f "key=$_signing_pub" &>/dev/null; then
                    echo "==> Signing key registered with GitHub."
                else
                    echo "==> Failed to register signing key via API. Add it manually at the URL above."
                fi
            else
                echo "==> Skipped. Add the key manually at the URL above if needed."
            fi
        else
            echo "==> Non-interactive shell — add the key manually at the URL above if needed."
        fi
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
    if ! _is_mounted; then
        echo "==> Mount '$VM_MOUNT' not found — run 'scripts/sandbox.sh up' first to mount the project."
        exit 1
    fi
    _env_args=("PROJECT_ROOT=$VM_MOUNT")
    [ -n "${GITHUB_TOKEN:-}" ] && _env_args+=("GH_TOKEN=$GITHUB_TOKEN" "GITHUB_TOKEN=$GITHUB_TOKEN")
    [ -n "${CHARMHUB_API_URL:-}" ] && _env_args+=("CHARMHUB_API_URL=$CHARMHUB_API_URL")
    [ -n "${SNAPCRAFT_API_URL:-}" ] && _env_args+=("SNAPCRAFT_API_URL=$SNAPCRAFT_API_URL")
    exec multipass exec "$VM_NAME" -- env "${_env_args[@]}" bash -lc "
        cd '$VM_MOUNT' && exec bash -l
    "
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

    # Resolve and validate SANDBOX_MCP_CONFIG_FILE path early — no VM work yet.
    if [ -n "${SANDBOX_MCP_CONFIG_FILE:-}" ]; then
        # Resolve relative paths against the project root for consistency.
        if [[ "$SANDBOX_MCP_CONFIG_FILE" != /* ]]; then
            SANDBOX_MCP_CONFIG_FILE="$PROJECT_DIR/$SANDBOX_MCP_CONFIG_FILE"
        fi
        if [ ! -f "$SANDBOX_MCP_CONFIG_FILE" ]; then
            echo "ERROR: SANDBOX_MCP_CONFIG_FILE not found: $SANDBOX_MCP_CONFIG_FILE" >&2
            exit 1
        fi
    fi

    # Validate the task argument for autonomous mode before any VM interaction.
    if [ "$INTERACTIVE" = "false" ]; then
        [ -n "$TASK" ] || { echo "Usage: scripts/sandbox.sh run 'task description'" >&2; exit 1; }
    fi

    if ! _is_mounted; then
        echo "==> Mount '$VM_MOUNT' not found — run 'scripts/sandbox.sh up' first to mount the project."
        exit 1
    fi

    # All prerequisites met. Copy MCP config into the VM now.
    _mcp_vm_file=""
    if [ -n "${SANDBOX_MCP_CONFIG_FILE:-}" ]; then
        echo "==> Copying MCP config into VM..."
        _mcp_vm_file=$(multipass exec "$VM_NAME" -- bash -c "mktemp /tmp/mcp-config-XXXXXX.json")
        # Install a preliminary trap for the MCP file in case PROMPT_FILE creation fails.
        # shellcheck disable=SC2064
        trap "multipass exec '$VM_NAME' -- rm -f '$_mcp_vm_file' 2>/dev/null || true" EXIT
        multipass exec "$VM_NAME" -- bash -c "cat > '$_mcp_vm_file'" < "$SANDBOX_MCP_CONFIG_FILE"
    fi

    PROMPT_FILE=$(multipass exec "$VM_NAME" -- bash -c "mktemp /tmp/copilot-prompt-XXXXXX")
    # Update the EXIT trap to cover PROMPT_FILE as well. This replaces the MCP-only
    # trap (if set) so both files are removed on any early exit due to set -euo pipefail.
    # shellcheck disable=SC2064
    trap "multipass exec '$VM_NAME' -- rm -f '$PROMPT_FILE' '$_mcp_vm_file' 2>/dev/null || true" EXIT

    {
        cat "$PROJECT_DIR/.agents/skills/system.md"
        if [ -n "$TASK" ]; then
            printf "\n\n---\n\nTask: %s\n" "$TASK"
        fi
    } | multipass exec "$VM_NAME" -- bash -c "cat > \"$PROMPT_FILE\""

    if [ "$INTERACTIVE" = "true" ]; then
        CONTEXT_MSG="Please read $PROMPT_FILE for project context, then await my instructions."
        # Build env var array, only including GH_TOKEN/GITHUB_TOKEN if they are actually set
        _env_args=()
        [ -n "${GITHUB_TOKEN:-}" ] && _env_args+=("GH_TOKEN=$GITHUB_TOKEN" "GITHUB_TOKEN=$GITHUB_TOKEN")
        [ -n "${CHARMHUB_API_URL:-}" ] && _env_args+=("CHARMHUB_API_URL=$CHARMHUB_API_URL")
        [ -n "${SNAPCRAFT_API_URL:-}" ] && _env_args+=("SNAPCRAFT_API_URL=$SNAPCRAFT_API_URL")
        _env_args+=("COPILOT_GITHUB_TOKEN=$_copilot_token" "COPILOT_MODEL=$COPILOT_MODEL" "PROJECT_ROOT=$VM_MOUNT")
        [ -n "$_mcp_vm_file" ] && _env_args+=("SANDBOX_MCP_VM_CONFIG=$_mcp_vm_file")
        multipass exec "$VM_NAME" -- env "${_env_args[@]}" bash -lc "
                cd '$VM_MOUNT'
            _mcp_extra=()
            [ -n \"\${SANDBOX_MCP_VM_CONFIG:-}\" ] && _mcp_extra=(--additional-mcp-config \"@\${SANDBOX_MCP_VM_CONFIG}\")
            _ec=0
            copilot --yolo \"\${_mcp_extra[@]}\" -i \"$CONTEXT_MSG\" || _ec=\$?
            rm -f \"$PROMPT_FILE\"
            exit \$_ec
        "
    else
        # Build env var array, only including GH_TOKEN/GITHUB_TOKEN if they are actually set
        _env_args=()
        [ -n "${GITHUB_TOKEN:-}" ] && _env_args+=("GH_TOKEN=$GITHUB_TOKEN" "GITHUB_TOKEN=$GITHUB_TOKEN")
        [ -n "${CHARMHUB_API_URL:-}" ] && _env_args+=("CHARMHUB_API_URL=$CHARMHUB_API_URL")
        [ -n "${SNAPCRAFT_API_URL:-}" ] && _env_args+=("SNAPCRAFT_API_URL=$SNAPCRAFT_API_URL")
        _env_args+=("COPILOT_GITHUB_TOKEN=$_copilot_token" "COPILOT_MODEL=$COPILOT_MODEL" "PROJECT_ROOT=$VM_MOUNT")
        [ -n "$_mcp_vm_file" ] && _env_args+=("SANDBOX_MCP_VM_CONFIG=$_mcp_vm_file")
        multipass exec "$VM_NAME" -- env "${_env_args[@]}" bash -lc "
                cd '$VM_MOUNT'
            _mcp_extra=()
            [ -n \"\${SANDBOX_MCP_VM_CONFIG:-}\" ] && _mcp_extra=(--additional-mcp-config \"@\${SANDBOX_MCP_VM_CONFIG}\")
            _ec=0
            copilot --yolo \"\${_mcp_extra[@]}\" -p \"\$(cat \"$PROMPT_FILE\")\" || _ec=\$?
            rm -f \"$PROMPT_FILE\"
            exit \$_ec
        "
    fi
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
case "${1:-}" in
    up)
        shift
        _cmd_up "$@"
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
