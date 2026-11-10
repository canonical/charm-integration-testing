#!/bin/bash
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
TEST_DIR="$(mktemp -d)"
trap 'rm -rf -- "$TEST_DIR"' EXIT

# Run a separate checkout with mocked VM and Copilot commands, not the user's VM.
mkdir -p "$TEST_DIR/scripts" "$TEST_DIR/.agents/skills"
cp "$PROJECT_DIR/scripts/sandbox.sh" "$TEST_DIR/scripts/"
printf 'Test project context\n' > "$TEST_DIR/.agents/skills/system.md"
while IFS= read -r name; do
    unset "$name"
done < <(compgen -v SANDBOX_)
export SANDBOX_VM=test-sandbox SANDBOX_MOUNT="$TEST_DIR"
export TEST_COPILOT_ARGS="$TEST_DIR/args"

gh() {
    return 1
}

multipass() {
    case "$1" in
        info)
            printf '{"info":{"%s":{"state":"Running","mounts":{"%s":{}}}}}\n' \
                "$SANDBOX_VM" "$SANDBOX_MOUNT"
            ;;
        exec)
            shift 3
            "$@"
            ;;
        *)
            echo "Unexpected multipass command: $1" >&2
            return 1
            ;;
    esac
}

copilot() {
    printf '%s\0' "$@" > "$TEST_COPILOT_ARGS"
    return "${TEST_COPILOT_EXIT:-0}"
}
export -f gh multipass copilot

run() {
    if ! bash "$TEST_DIR/scripts/sandbox.sh" run "$@" > "$TEST_DIR/output" 2>&1; then
        cat "$TEST_DIR/output" >&2
        return 1
    fi
    mapfile -d '' -t actual < "$TEST_COPILOT_ARGS"
}

assert_extra() {
    local expected=("$@") index
    [ "${#actual[@]}" -eq "$((3 + ${#expected[@]}))" ]
    [ "${actual[0]}" = --yolo ]
    for ((index = 0; index < ${#expected[@]}; index++)); do
        [ "${actual[index + 3]}" = "${expected[index]}" ]
    done
}

run --interactive
assert_extra
[ "${actual[1]}" = -i ]
[[ "${actual[2]}" == "Please read "*"/copilot-prompt-"* ]]

run --interactive -- --resume
assert_extra --resume

run --interactive -- --resume "Session's name with spaces"
assert_extra --resume "Session's name with spaces"

run --interactive -- -r session-id
assert_extra -r session-id

run --interactive -- --continue
assert_extra --continue

run --interactive -- --model auto --name "\$HOME; *" --empty ''
assert_extra --model auto --name "\$HOME; *" --empty ''

run finish this task -- --continue
assert_extra --continue
[ "${actual[1]}" = -p ]
[[ "${actual[2]}" == *"Task: finish this task" ]]

run original task
assert_extra
[[ "${actual[2]}" == *"Task: original task" ]]

if TEST_COPILOT_EXIT=42 bash "$TEST_DIR/scripts/sandbox.sh" run --interactive -- --continue \
    > "$TEST_DIR/output" 2>&1; then
    echo "Expected Copilot's exit status to be propagated." >&2
    exit 1
else
    [ "$?" -eq 42 ]
fi

echo "Sandbox argument tests passed."
