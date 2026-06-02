#!/bin/bash
# Host-side launcher that runs Copilot inside the validator VM.
#
# Host responsibility:
# - Read GITHUB_TOKEN from validator-development-sandbox/.env
# - Pass the project context into the VM
#
# VM responsibility:
# - Run Copilot, install deps, and modify the mounted project
#
# Authentication:
#   GITHUB_TOKEN must be set in validator-development-sandbox/.env before running.
#   It is passed to gh CLI inside the VM as a restricted fine-grained PAT.
#   Copilot AI auth uses COPILOT_GITHUB_TOKEN if set, otherwise falls back to
#   'gh auth token' on the host.
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
# Environment / .env (optional):
#   GITHUB_TOKEN          Fine-grained PAT for gh CLI (contents:read, pull_requests:write)
#   COPILOT_GITHUB_TOKEN  Override for Copilot AI auth (default: gh auth token)
#   VALIDATOR_VM          VM name override (default: validator-k8s)
#   COPILOT_MODEL         Model override (default: sonnet-4.6)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEV_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Fetch the host Copilot token BEFORE sourcing .env so GITHUB_TOKEN in .env
# does not affect the lookup.
_gh_token=$(gh auth token 2>/dev/null || true)

# Load .env - required to supply GITHUB_TOKEN; optionally COPILOT_GITHUB_TOKEN.
if [ -f "$DEV_DIR/.env" ]; then
    set -a
    # shellcheck source=/dev/null
    source "$DEV_DIR/.env"
    set +a
fi

VM_NAME="${VALIDATOR_VM:-validator-k8s}"
COPILOT_MODEL="${COPILOT_MODEL:-sonnet-4.6}"

# Copilot AI auth: use explicit override if set, otherwise fall back to host token.
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
        GH_TOKEN='$GITHUB_TOKEN' GITHUB_TOKEN='$GITHUB_TOKEN' COPILOT_GITHUB_TOKEN='$_copilot_token' \
        COPILOT_MODEL='$COPILOT_MODEL' copilot --yolo \
            -i \"$CONTEXT_MSG\"
        rm -f $PROMPT_FILE
    "
else
    [ -n "$TASK" ] || { echo "Usage: validator-development-sandbox/bin/run.sh 'task description'"; exit 1; }
    multipass exec "$VM_NAME" -- bash -lc "
        cd /project
        GH_TOKEN='$GITHUB_TOKEN' GITHUB_TOKEN='$GITHUB_TOKEN' COPILOT_GITHUB_TOKEN='$_copilot_token' \
        COPILOT_MODEL='$COPILOT_MODEL' copilot --yolo \
            -p \"\$(cat $PROMPT_FILE)\"
        rm -f $PROMPT_FILE
    "
fi
