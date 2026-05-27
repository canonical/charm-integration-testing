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
# Authentication uses the host's GitHub token via 'gh auth token' (system keyring).
# No credentials are written inside the VM.
#
# Usage:
#   validator-development-sandbox/bin/run.sh 'your task here'          # autonomous mode
#   validator-development-sandbox/bin/run.sh --interactive             # interactive mode
#   validator-development-sandbox/bin/run.sh --task develop <name>     # develop a new validator
#   validator-development-sandbox/bin/run.sh --task test <name>        # test an existing validator
#
# Environment:
#   VALIDATOR_VM     VM name override (default: validator-k8s)
#   COPILOT_MODEL    Model override (default: sonnet-4.6)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEV_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VM_NAME="${VALIDATOR_VM:-validator-k8s}"
COPILOT_MODEL="${COPILOT_MODEL:-sonnet-4.6}"

# Fetch GitHub token from the host's system keyring via gh CLI.
# Injected inline into the bash command so no token is written to disk in the VM.
_gh_token=$(gh auth token 2>/dev/null || true)
if [ -z "$_gh_token" ]; then
    echo "Warning: 'gh auth token' returned nothing."
    echo "Run: gh auth login   on this machine first."
fi

INTERACTIVE=false
TASK_TYPE=""
TASK_VALIDATOR=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --interactive)
            INTERACTIVE=true
            shift
            ;;
        --task)
            TASK_TYPE="${2:-}"
            TASK_VALIDATOR="${3:-}"
            if [ -z "$TASK_TYPE" ] || [ -z "$TASK_VALIDATOR" ]; then
                echo "Usage: bin/run.sh --task <develop|test> <validator-name>" >&2
                exit 1
            fi
            case "$TASK_TYPE" in
                develop|test) ;;
                *) echo "Unknown task type: $TASK_TYPE (use 'develop' or 'test')" >&2; exit 1 ;;
            esac
            shift 3
            ;;
        -h|--help)
            cat <<'EOF'
Usage:
  bin/run.sh 'task description'                    autonomous mode
  bin/run.sh --interactive                         interactive mode
  bin/run.sh --task develop <name>                 develop a new validator (autonomous)
  bin/run.sh --task test <name>                    test an existing validator (autonomous)
  bin/run.sh --task develop <name> --interactive   develop step-by-step with check-ins
  bin/run.sh --task test <name> --interactive      test step-by-step with check-ins
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

if [ -n "$TASK_TYPE" ]; then
    TASK_PROMPT=$(cat "$DEV_DIR/prompts/${TASK_TYPE}-validator.md")
    TASK="${TASK_PROMPT}"$'\n\n'"Validator name: ${TASK_VALIDATOR}"
else
    TASK="${*:-}"
fi

# Build the full prompt from system.md + task
PROMPT_FILE=$(multipass exec "$VM_NAME" -- bash -c "mktemp /tmp/copilot-prompt-XXXXXX")

{
    cat "$DEV_DIR/prompts/system.md"
    if [ -n "$TASK" ]; then
        printf "\n\n---\n\nTask: %s\n" "$TASK"
    fi
} | multipass exec "$VM_NAME" -- bash -c "cat > $PROMPT_FILE"

if [ "$INTERACTIVE" = "true" ]; then
    if [ -n "$TASK_TYPE" ]; then
        CONTEXT_MSG="Please read $PROMPT_FILE for your task instructions. Work through the task step by step. After completing each major step, briefly summarize what you did and what comes next, then pause and wait for confirmation or redirection before continuing."
    else
        CONTEXT_MSG="Please read $PROMPT_FILE for project context, then await my instructions."
    fi
    multipass exec "$VM_NAME" -- bash -lc "
        cd /project
        GH_TOKEN='$_gh_token' GITHUB_TOKEN='$_gh_token' \
        COPILOT_MODEL='$COPILOT_MODEL' copilot --yolo \
            -i \"$CONTEXT_MSG\"
        rm -f $PROMPT_FILE
    "
else
    [ -n "$TASK" ] || { echo "Usage: validator-development-sandbox/bin/run.sh 'task description'"; exit 1; }
    multipass exec "$VM_NAME" -- bash -lc "
        cd /project
        GH_TOKEN='$_gh_token' GITHUB_TOKEN='$_gh_token' \
        COPILOT_MODEL='$COPILOT_MODEL' copilot --yolo \
            -p \"\$(cat $PROMPT_FILE)\"
        rm -f $PROMPT_FILE
    "
fi
