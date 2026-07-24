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
#   TEST_OBSERVER_API_URL   Test Observer API URL, required by --with-test-observer-mcp
#   TEST_OBSERVER_API_KEY   Test Observer API key, optional (read-only tools work without it)
#   TEST_OBSERVER_MCP_PORT  Port for the test-observer-mcp server inside the VM (default: 8090)

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
TEST_OBSERVER_MCP_REPO="canonical/test-observer-mcp"
TEST_OBSERVER_MCP_PORT="${TEST_OBSERVER_MCP_PORT:-8090}"
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
  scripts/sandbox.sh run --with-test-observer-mcp ...
                                                Also launch test-observer-mcp for this session
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
  TEST_OBSERVER_API_URL   Test Observer API URL, required by --with-test-observer-mcp
  TEST_OBSERVER_API_KEY   Test Observer API key, optional (read-only tools work without it)
  TEST_OBSERVER_MCP_PORT  Port for the test-observer-mcp server inside the VM (default: 8090)

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
# Helper: clone (on the host, via SSH) and launch test-observer-mcp inside
# the VM.
#
# Cloning happens on the host so it can use the host's own SSH agent/keys
# (multipass VMs have no access to the host's SSH identity or agent). The
# clone lands in a fixed directory under the project root, which is already
# bind-mounted into the VM via `multipass mount`, so no separate transfer
# step is needed -- the cloned tree is visible inside the VM immediately at
# the same relative path.
#
# Sets the caller's _to_mcp_host_dir (host clone dir; kept after teardown so
# the clone can be reused/inspected -- only the running server is stopped),
# _to_mcp_dir (VM-side path, for teardown), and _to_mcp_vm_file (generated
# MCP config JSON, for --additional-mcp-config) on success. Exits the script
# on failure.
# ---------------------------------------------------------------------------
_start_test_observer_mcp() {
    echo "==> Cloning test-observer-mcp on the host (via SSH)..."
    _to_mcp_host_dir="$PROJECT_DIR/.test-observer-mcp"
    rm -rf "$_to_mcp_host_dir"
    if ! git clone --depth 1 --quiet "git@github.com:${TEST_OBSERVER_MCP_REPO}.git" "$_to_mcp_host_dir"; then
        echo "ERROR: Failed to clone canonical/test-observer-mcp on the host via SSH." >&2
        echo "       Ensure your SSH key is added to ssh-agent and registered with GitHub." >&2
        rm -rf "$_to_mcp_host_dir"
        _to_mcp_host_dir=""
        exit 1
    fi
    _to_mcp_dir="$VM_MOUNT/.test-observer-mcp"

    echo "==> Installing Go toolchain for test-observer-mcp (if needed)..."
    multipass exec "$VM_NAME" -- bash -c "cd '$_to_mcp_dir' && ./scripts/go/setup.sh"

    # `go run` spawns the compiled server binary as a *child* process rather
    # than exec'ing into it, so killing (or losing track of) a previous
    # `go run` wrapper does not kill the server it launched -- that binary
    # is orphaned and keeps holding $TEST_OBSERVER_MCP_PORT. A later `run`
    # invocation then fails to bind the port and exits almost immediately,
    # which surfaces as a confusing "process did not start" error with an
    # empty-looking server.log. Proactively clear out anything left over
    # from a previous session before starting a new one.
    # `multipass exec` runs commands inside a login session managed by
    # systemd-logind. Without lingering enabled for the user, logind kills
    # every process in that session's cgroup (including anything started
    # with `nohup`/`setsid`/`disown`) the moment the exec channel closes --
    # so the server would be reaped right after being launched below, even
    # though it appears to start fine while the launching call is still
    # connected. Enabling lingering makes systemd keep the user's processes
    # (and its user-manager instance) running independently of any session.
    echo "==> Ensuring background processes can survive detached sessions (systemd linger)..."
    multipass exec "$VM_NAME" -- bash -c "loginctl show-user ubuntu 2>/dev/null | grep -q '^Linger=yes' || sudo loginctl enable-linger ubuntu"

    echo "==> Clearing any leftover test-observer-mcp process on port $TEST_OBSERVER_MCP_PORT..."
    # The pkill patterns use a bracket around one character (e.g. '[c]md' instead
    # of 'cmd') so the regex does not also match the literal pattern text
    # embedded in this very `bash -c "..."` invocation's own argv -- without it,
    # `pkill -f` would self-match and SIGKILL the shell running the cleanup
    # before it reaches `true`, making `multipass exec` exit 255 and, combined
    # with `set -e` in the outer script, abort the whole sandbox run.
    multipass exec "$VM_NAME" -- bash -c "fuser -k ${TEST_OBSERVER_MCP_PORT}/tcp 2>/dev/null; pkill -9 -f '[c]md/server/main.go --dangerously-disable-auth' 2>/dev/null; pkill -9 -f '/[m]ain --dangerously-disable-auth' 2>/dev/null; true"

    echo "==> Starting test-observer-mcp on port $TEST_OBSERVER_MCP_PORT..."
    # Backgrounding the remote command from *inside* a non-interactive
    # `bash -c` (e.g. `cmd & disown`) is racy: that shell returns -- closing
    # multipass's exec channel -- almost immediately after backgrounding,
    # often before the forked job has finished detaching, so the server
    # frequently never actually starts even though the launch command itself
    # reports success. Backgrounding the `multipass exec` client itself on
    # the *host* side instead is reliable: the remote command runs in the
    # foreground (from multipass's point of view), which is the same
    # reliable mode used by the health checks below, while the host script
    # doesn't block on it. `setsid` still gives the remote process its own
    # session so it survives if the connection ever drops, and the systemd
    # linger fix above keeps it alive regardless. The log is redirected via
    # the host-side clone dir ($_to_mcp_host_dir) rather than the VM-side
    # path ($_to_mcp_dir) since the redirect is evaluated by the host shell
    # -- both paths point at the same underlying files thanks to the
    # `multipass mount` bind mount.
    multipass exec "$VM_NAME" -- env \
        TEST_OBSERVER_API_URL="$TEST_OBSERVER_API_URL" \
        TEST_OBSERVER_API_KEY="${TEST_OBSERVER_API_KEY:-}" \
        PORT="$TEST_OBSERVER_MCP_PORT" \
        bash -c "cd '$_to_mcp_dir' && exec setsid ./scripts/run.sh" \
        < /dev/null > "$_to_mcp_host_dir/server.log" 2>&1 &
    disown

    # Confirm the background process actually launched before burning up to
    # 10 minutes on the health-check loop below. `go run`'s own PID is
    # nested several process layers below the `setsid`/`nohup` wrapper we
    # just backgrounded (shell -> setsid -> nohup -> run.sh -> go run), so
    # capturing and polling a single PID via `$!` is unreliable -- the
    # wrapper layer we'd capture often exits right after forking even
    # though the actual server keeps running. Polling for log output is a
    # more direct signal: a real startup failure (bad shebang, missing
    # script, port conflict, Go toolchain issue) writes to server.log
    # almost immediately, so this still fails fast without depending on any
    # particular PID staying alive.
    echo "==> Verifying test-observer-mcp process started..."
    _to_mcp_started=false
    for _ in $(seq 1 5); do
        sleep 2
        if multipass exec "$VM_NAME" -- test -s "$_to_mcp_dir/server.log"; then
            _to_mcp_started=true
            break
        fi
    done
    if [ "$_to_mcp_started" = "false" ]; then
        echo "ERROR: test-observer-mcp process did not start (server.log is empty or missing)." >&2
        if multipass exec "$VM_NAME" -- test -f "$_to_mcp_dir/server.log"; then
            echo "Server log:" >&2
            multipass exec "$VM_NAME" -- tail -n 50 "$_to_mcp_dir/server.log" >&2 || true
        else
            echo "Server log was never created at $_to_mcp_dir/server.log." >&2
            multipass exec "$VM_NAME" -- ls -la "$_to_mcp_dir" >&2 || true
        fi
        echo "Hint: check for a stale process still bound to port $TEST_OBSERVER_MCP_PORT:" >&2
        echo "  multipass exec $VM_NAME -- ss -ltnp | grep $TEST_OBSERVER_MCP_PORT" >&2
        exit 1
    fi
    echo "==> test-observer-mcp is producing output; waiting for it to become healthy."

    echo "==> Waiting for test-observer-mcp health check..."
    # `./scripts/run.sh` invokes `go run`, which on first launch must fetch
    # all Go module dependencies before the server starts listening. 120s
    # (60 * 2s) was not enough time for that initial fetch to complete, so
    # the health check was timing out spuriously. Allow up to 10 minutes.
    _to_mcp_ready=false
    for _ in $(seq 1 300); do
        if multipass exec "$VM_NAME" -- curl -sf "http://localhost:$TEST_OBSERVER_MCP_PORT/health" >/dev/null 2>&1; then
            _to_mcp_ready=true
            break
        fi
        sleep 2
    done
    if [ "$_to_mcp_ready" = "false" ]; then
        echo "ERROR: test-observer-mcp did not become healthy in time." >&2
        if multipass exec "$VM_NAME" -- test -f "$_to_mcp_dir/server.log"; then
            echo "Server log:" >&2
            multipass exec "$VM_NAME" -- tail -n 50 "$_to_mcp_dir/server.log" >&2 || true
        else
            echo "Server log was never created at $_to_mcp_dir/server.log -- the process" >&2
            echo "likely never started. Directory contents:" >&2
            multipass exec "$VM_NAME" -- ls -la "$_to_mcp_dir" >&2 || true
            echo "Process check (pgrep run.sh):" >&2
            multipass exec "$VM_NAME" -- pgrep -af run.sh >&2 || true
        fi
        exit 1
    fi
    echo "==> test-observer-mcp is healthy."

    _to_mcp_vm_file=$(multipass exec "$VM_NAME" -- mktemp /tmp/mcp-test-observer-XXXXXX.json)
    multipass exec "$VM_NAME" -- bash -c "cat > '$_to_mcp_vm_file'" <<EOF
{
  "mcpServers": {
    "test-observer": {
      "type": "http",
      "url": "http://localhost:$TEST_OBSERVER_MCP_PORT/mcp",
      "tools": ["*"]
    }
  }
}
EOF
}

# ---------------------------------------------------------------------------
# Subcommand: run
# ---------------------------------------------------------------------------
_cmd_run() {
    COPILOT_MODEL="${COPILOT_MODEL:-sonnet-4.6}"
    _copilot_token="${COPILOT_GITHUB_TOKEN:-$_gh_token}"

    INTERACTIVE=false
    WITH_TEST_OBSERVER_MCP=false
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --interactive)
                INTERACTIVE=true
                shift
                ;;
            --with-test-observer-mcp)
                WITH_TEST_OBSERVER_MCP=true
                shift
                ;;
            -h|--help)
                cat <<'EOF'
Usage:
  scripts/sandbox.sh run 'task description'   autonomous mode
  scripts/sandbox.sh run --interactive        interactive mode
  scripts/sandbox.sh run --with-test-observer-mcp ...
                                               also launch test-observer-mcp for this session
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

    # Fail fast on missing test-observer-mcp prerequisites before any VM work.
    if [ "$WITH_TEST_OBSERVER_MCP" = "true" ] && [ -z "${TEST_OBSERVER_API_URL:-}" ]; then
        echo "ERROR: TEST_OBSERVER_API_URL must be set (in development-sandbox/.env or the" >&2
        echo "       environment) to use --with-test-observer-mcp." >&2
        exit 1
    fi

    # Validate the task argument for autonomous mode before any VM interaction.
    if [ "$INTERACTIVE" = "false" ]; then
        [ -n "$TASK" ] || { echo "Usage: scripts/sandbox.sh run 'task description'" >&2; exit 1; }
    fi

    if ! _is_mounted; then
        echo "==> Mount '$VM_MOUNT' not found — run 'scripts/sandbox.sh up' first to mount the project."
        exit 1
    fi

    # Tear down every resource this run may have created, regardless of which
    # step failed. Safe to call at any point since all paths default to empty.
    _mcp_vm_file=""
    _to_mcp_host_dir=""
    _to_mcp_dir=""
    _to_mcp_vm_file=""
    PROMPT_FILE=""
    _cleanup_run() {
        _rm_args=()
        [ -n "$PROMPT_FILE" ] && _rm_args+=("$PROMPT_FILE")
        [ -n "$_mcp_vm_file" ] && _rm_args+=("$_mcp_vm_file")
        [ -n "$_to_mcp_vm_file" ] && _rm_args+=("$_to_mcp_vm_file")
        if [ "${#_rm_args[@]}" -gt 0 ]; then
            multipass exec "$VM_NAME" -- rm -f "${_rm_args[@]}" 2>/dev/null || true
        fi
        if [ -n "$_to_mcp_dir" ]; then
            multipass exec "$VM_NAME" -- bash -c "fuser -k ${TEST_OBSERVER_MCP_PORT}/tcp 2>/dev/null" 2>/dev/null || true
        fi
    }
    trap _cleanup_run EXIT

    # All prerequisites met. Copy MCP config into the VM now.
    if [ -n "${SANDBOX_MCP_CONFIG_FILE:-}" ]; then
        echo "==> Copying MCP config into VM..."
        _mcp_vm_file=$(multipass exec "$VM_NAME" -- bash -c "mktemp /tmp/mcp-config-XXXXXX.json")
        multipass exec "$VM_NAME" -- bash -c "cat > '$_mcp_vm_file'" < "$SANDBOX_MCP_CONFIG_FILE"
    fi

    if [ "$WITH_TEST_OBSERVER_MCP" = "true" ]; then
        _start_test_observer_mcp
    fi

    PROMPT_FILE=$(multipass exec "$VM_NAME" -- bash -c "mktemp /tmp/copilot-prompt-XXXXXX")

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
        [ -n "$_to_mcp_vm_file" ] && _env_args+=("SANDBOX_TO_MCP_VM_CONFIG=$_to_mcp_vm_file")
        multipass exec "$VM_NAME" -- env "${_env_args[@]}" bash -lc "
                cd '$VM_MOUNT'
            _mcp_extra=()
            [ -n \"\${SANDBOX_MCP_VM_CONFIG:-}\" ] && _mcp_extra+=(--additional-mcp-config \"@\${SANDBOX_MCP_VM_CONFIG}\")
            [ -n \"\${SANDBOX_TO_MCP_VM_CONFIG:-}\" ] && _mcp_extra+=(--additional-mcp-config \"@\${SANDBOX_TO_MCP_VM_CONFIG}\")
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
        [ -n "$_to_mcp_vm_file" ] && _env_args+=("SANDBOX_TO_MCP_VM_CONFIG=$_to_mcp_vm_file")
        multipass exec "$VM_NAME" -- env "${_env_args[@]}" bash -lc "
                cd '$VM_MOUNT'
            _mcp_extra=()
            [ -n \"\${SANDBOX_MCP_VM_CONFIG:-}\" ] && _mcp_extra+=(--additional-mcp-config \"@\${SANDBOX_MCP_VM_CONFIG}\")
            [ -n \"\${SANDBOX_TO_MCP_VM_CONFIG:-}\" ] && _mcp_extra+=(--additional-mcp-config \"@\${SANDBOX_TO_MCP_VM_CONFIG}\")
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
