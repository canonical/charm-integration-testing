#!/bin/bash
# Host-side launcher that runs Copilot inside the validator VM.
#
# Host responsibility:
# - Read the host GitHub token from gh auth
# - Pass the project context into the VM
#
# VM responsibility:
# - Run Copilot, install deps, and modify the mounted project
#
# Authentication:
#   COPILOT_GITHUB_TOKEN  Full host token - used exclusively by the Copilot binary.
#   GH_TOKEN / GITHUB_TOKEN  Token for the gh CLI. Set GH_RESTRICTED_TOKEN in
#                            validator-development-sandbox/.env to use a fine-grained
#                            PAT (contents:read + pull_requests:write) here, keeping
#                            the full token isolated to Copilot AI auth only.
#   No credentials are written inside the VM.
#
# Usage:
#   validator-development-sandbox/bin/run.sh 'your task here'   # autonomous mode
#   validator-development-sandbox/bin/run.sh --interactive       # interactive mode
#
# Inside an interactive session use skill slash commands:
#   /develop-validator   -- develop a new validator
#   /test-validator      -- test an existing validator
#
# Skills are auto-discovered from .agents/skills/ (symlinked to prompts/).
#
# Environment / .env:
#   VALIDATOR_VM          VM name override (default: validator-k8s)
#   COPILOT_MODEL         Model override (default: sonnet-4.6)
#   COPILOT_GITHUB_TOKEN  Override for Copilot AI auth token (default: gh auth token)
#   GITHUB_TOKEN          Fine-grained PAT for gh CLI (default: gh auth token)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEV_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VM_NAME="${VALIDATOR_VM:-validator-k8s}"
COPILOT_MODEL="${COPILOT_MODEL:-sonnet-4.6}"

# Fetch the full GitHub token from the host keyring BEFORE sourcing .env,
# so that GITHUB_TOKEN in .env does not affect this lookup.
_gh_token=$(gh auth token 2>/dev/null || true)
if [ -z "$_gh_token" ]; then
    echo "Warning: 'gh auth token' returned nothing."
    echo "Run: gh auth login   on this machine first."
fi

# Load optional .env (gitignored) for GITHUB_TOKEN, COPILOT_GITHUB_TOKEN etc.
if [ -f "$DEV_DIR/.env" ]; then
    set -a
    # shellcheck source=/dev/null
    source "$DEV_DIR/.env"
    set +a
fi

# COPILOT_GITHUB_TOKEN: full token for Copilot AI auth.
#   Defaults to the host keyring token; set in .env to override.
_copilot_token="${COPILOT_GITHUB_TOKEN:-$_gh_token}"

# GITHUB_TOKEN: token for gh CLI inside the VM.
#   Set to a fine-grained PAT in .env to restrict gh CLI access.
#   Falls back to the full host token if not set.
_gh_cli_token="${GITHUB_TOKEN:-$_gh_token}"

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
  bin/run.sh 'task description'   autonomous mode
  bin/run.sh --interactive        interactive mode (use /develop-validator, /test-validator)
EOF
            exit 0
            ;;
        --)
            shift
            break
            ;;
        -*)
            echo "Unknown option: $1" >&2
            echo "Run bin/run.sh --help for usage." >&2
            exit 1
            ;;
        *)
            break
            ;;
    esac
done

TASK="${*:-}"

# Build the full prompt from system.md + task
PROMPT_FILE=$(multipass exec "$VM_NAME" -- bash -c "mktemp /tmp/copilot-prompt-XXXXXX")

{
    cat "$DEV_DIR/prompts/system.md"
    if [ -n "$TASK" ]; then
        printf "\n\n---\n\nTask: %s\n" "$TASK"
    fi
} | multipass exec "$VM_NAME" -- bash -c "cat > $PROMPT_FILE"

if [ "$INTERACTIVE" = "true" ]; then
    CONTEXT_MSG="Please read $PROMPT_FILE for project context, then await my instructions."
    multipass exec "$VM_NAME" -- bash -lc "
        cd /project
        COPILOT_GITHUB_TOKEN='$_copilot_token' GH_TOKEN='$_gh_cli_token' GITHUB_TOKEN='$_gh_cli_token' \
        COPILOT_MODEL='$COPILOT_MODEL' copilot --yolo \
            -i "$CONTEXT_MSG"
        rm -f $PROMPT_FILE
    "
else
    [ -n "$TASK" ] || { echo "Usage: validator-development-sandbox/bin/run.sh 'task description'"; exit 1; }
    multipass exec "$VM_NAME" -- bash -lc "
        cd /project
        COPILOT_GITHUB_TOKEN='$_copilot_token' GH_TOKEN='$_gh_cli_token' GITHUB_TOKEN='$_gh_cli_token' \
        COPILOT_MODEL='$COPILOT_MODEL' copilot --yolo \
            -p \"\$(cat $PROMPT_FILE)\"
        rm -f $PROMPT_FILE
    "
fi
