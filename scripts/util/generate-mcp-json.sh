#!/bin/bash
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Source this file to define generate_mcp_json and ensure_mcp_json, then call:
#
#   JIRA_API_TOKEN=... TEST_OBSERVER_API_KEY=... \
#       generate_mcp_json [--sample PATH] [--output PATH]
#
# Generates development-sandbox/mcp.json from mcp.json.sample.
#
# Each placeholder value (YOUR_...) in a server's "env" block is replaced with
# the host environment variable named by its key, e.g.
# "JIRA_API_TOKEN": "YOUR_JIRA_API_TOKEN" is filled from $JIRA_API_TOKEN.
# JIRA_USERNAME falls back to `git config user.email`.
#
# Idempotent: rewrites the output only when its content would change, and
# always leaves it owner-only (0600). Fails without touching the output if any
# placeholder cannot be resolved. Before writing, lists the variable names it
# will inject and asks for consent; declining fails without writing.

# shellcheck source-path=SCRIPTDIR source=confirm.sh
source "$(dirname "${BASH_SOURCE[0]}")/confirm.sh"

_MCP_SANDBOX_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../development-sandbox" && pwd)"

# Generate the MCP config at $1 only if it does not exist yet.
ensure_mcp_json() {
    local output="${1:-$_MCP_SANDBOX_DIR/mcp.json}"
    if [[ -f "$output" ]]; then
        echo "==> $output exists; leaving it unchanged."
        return 0
    fi
    generate_mcp_json --output "$output"
}

# Subshell body: strict mode, exits and traps stay contained, and a non-zero
# exit becomes the function's return status.
generate_mcp_json() (
    set -euo pipefail

    local sample="$_MCP_SANDBOX_DIR/mcp.json.sample" output="$_MCP_SANDBOX_DIR/mcp.json"

    die() { echo "ERROR: $*" >&2; exit 1; }

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --sample) sample="${2:?--sample requires a path}"; shift 2 ;;
            --output) output="${2:?--output requires a path}"; shift 2 ;;
            -h|--help) sed -n '/^# Source this file/,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
            *) die "unknown argument: $1" ;;
        esac
    done

    command -v jq >/dev/null 2>&1 || die "jq is required."
    [[ -f "$sample" ]] || die "sample not found: $sample"

    jq -e '.mcpServers | type == "object" and length > 0' "$sample" >/dev/null \
        || die "$sample has no 'mcpServers' entries."

    if [[ -z "${JIRA_USERNAME:-}" ]]; then
        JIRA_USERNAME="$(git config user.email 2>/dev/null || true)"
        export JIRA_USERNAME
        [[ -n "$JIRA_USERNAME" ]] && echo "==> JIRA_USERNAME set from git config user.email"
    fi

    # Collect env vars for all placeholders, as tab-separated "server<TAB>key<TAB>placeholder".
    local placeholders=()
    mapfile -t placeholders < <(jq -r '
        .mcpServers | to_entries[] | .key as $server
        | (.value.env // {}) | to_entries[]
        | select(.value | type == "string" and startswith("YOUR_"))
        | [$server, .key, .value] | @tsv' "$sample")

    local missing=() injected=() values='{}' entry server key placeholder value
    for entry in "${placeholders[@]}"; do
        IFS=$'\t' read -r server key placeholder <<< "$entry"
        [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || die "invalid env key '$key' in server '$server'."
        value="$(printenv "$key" || true)"
        if [[ -z "$value" ]]; then
            missing+=("$key (server '$server', placeholder $placeholder)")
            continue
        fi
        injected+=("$key (server '$server')")
        values="$(jq --arg k "$key" --arg v "$value" '. + {($k): $v}' <<< "$values")"
    done

    if [[ ${#missing[@]} -gt 0 ]]; then
        echo "ERROR: missing required variables:" >&2
        printf '  - %s\n' "${missing[@]}" >&2
        echo "Export them in your shell and re-run." >&2
        exit 1
    fi

    local config leftovers servers
    config="$(jq --argjson vals "$values" '
        .mcpServers |= map_values(
            if .env then .env |= with_entries(
                .key as $k | if (.value | type == "string" and startswith("YOUR_")) and ($vals | has($k))
                then .value = $vals[$k] else . end)
            else . end)' "$sample")"

    leftovers="$(jq -r 'paths(type == "string" and contains("YOUR_")) | map(tostring) | join(".")' <<< "$config")"
    [[ -z "$leftovers" ]] || die "unresolved placeholders:"$'\n'"$leftovers"

    servers="$(jq '.mcpServers | length' <<< "$config")"
    if [[ -f "$output" ]] && [[ "$(cat "$output")" == "$config" ]]; then
        chmod 600 "$output"
        echo "==> $output is up to date ($servers server(s))."
    else
        {
            echo "WARNING: these variables from your environment will be written to"
            echo "$output and injected into the sandbox's MCP servers. Anything in the"
            echo "sandbox, including the AI agent, can read them:"
            printf '  - %s\n' "${injected[@]}"
        } >&2
        confirm "Write $output?" || die "not writing $output without your consent."

        # Contains credentials: write owner-only to a temp file, then swap it in atomically.
        local tmp action
        tmp="$(mktemp "$output.XXXXXX")"
        trap 'rm -f "$tmp"' EXIT
        printf '%s\n' "$config" > "$tmp"
        chmod 600 "$tmp"
        action="Created"
        [[ -e "$output" ]] && action="Updated"
        mv -f "$tmp" "$output"
        trap - EXIT
        echo "==> $action $output ($servers server(s))."
    fi
)
