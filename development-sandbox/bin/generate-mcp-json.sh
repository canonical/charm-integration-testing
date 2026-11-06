#!/bin/bash
# Generate development-sandbox/mcp.json from mcp.json.sample.
#
# Each placeholder value (YOUR_...) in a server's "env" block is replaced with
# the host environment variable named by its key, e.g.
# "JIRA_API_TOKEN": "YOUR_JIRA_API_TOKEN" is filled from $JIRA_API_TOKEN.
# JIRA_USERNAME falls back to `git config user.email`.
#
# Idempotent: rewrites the output only when its content would change, and
# always leaves it owner-only (0600). Fails without touching the output if any
# placeholder cannot be resolved.
#
# Usage:
#   JIRA_API_TOKEN=... TEST_OBSERVER_API_KEY=... \
#       development-sandbox/bin/generate-mcp-json.sh [--sample PATH] [--output PATH]

set -euo pipefail

SANDBOX_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SAMPLE="$SANDBOX_DIR/mcp.json.sample"
OUTPUT="$SANDBOX_DIR/mcp.json"

die() { echo "ERROR: $*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --sample) SAMPLE="${2:?--sample requires a path}"; shift 2 ;;
        --output) OUTPUT="${2:?--output requires a path}"; shift 2 ;;
        -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done

command -v jq >/dev/null 2>&1 || die "jq is required."
[[ -f "$SAMPLE" ]] || die "sample not found: $SAMPLE"

jq -e '.mcpServers | type == "object" and length > 0' "$SAMPLE" >/dev/null \
    || die "$SAMPLE has no 'mcpServers' entries."

if [[ -z "${JIRA_USERNAME:-}" ]]; then
    JIRA_USERNAME="$(git config user.email 2>/dev/null || true)"
    export JIRA_USERNAME
    [[ -n "$JIRA_USERNAME" ]] && echo "==> JIRA_USERNAME set from git config user.email"
fi

# Collect env vars for all placeholders, as tab-separated "server<TAB>key<TAB>placeholder".
mapfile -t placeholders < <(jq -r '
    .mcpServers | to_entries[] | .key as $server
    | (.value.env // {}) | to_entries[]
    | select(.value | type == "string" and startswith("YOUR_"))
    | [$server, .key, .value] | @tsv' "$SAMPLE")

missing=()
values='{}'
for entry in "${placeholders[@]}"; do
    IFS=$'\t' read -r server key placeholder <<< "$entry"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || die "invalid env key '$key' in server '$server'."
    value="$(printenv "$key" || true)"
    if [[ -z "$value" ]]; then
        missing+=("$key (server '$server', placeholder $placeholder)")
        continue
    fi
    values="$(jq --arg k "$key" --arg v "$value" '. + {($k): $v}' <<< "$values")"
done

if [[ ${#missing[@]} -gt 0 ]]; then
    echo "ERROR: missing required variables:" >&2
    printf '  - %s\n' "${missing[@]}" >&2
    echo "Export them in your shell and re-run." >&2
    exit 1
fi

config="$(jq --argjson vals "$values" '
    .mcpServers |= map_values(
        if .env then .env |= with_entries(
            .key as $k | if (.value | type == "string" and startswith("YOUR_")) and ($vals | has($k))
            then .value = $vals[$k] else . end)
        else . end)' "$SAMPLE")"

leftovers="$(jq -r 'paths(type == "string" and contains("YOUR_")) | map(tostring) | join(".")' <<< "$config")"
[[ -z "$leftovers" ]] || die "unresolved placeholders:"$'\n'"$leftovers"

servers="$(jq '.mcpServers | length' <<< "$config")"
if [[ -f "$OUTPUT" ]] && [[ "$(cat "$OUTPUT")" == "$config" ]]; then
    chmod 600 "$OUTPUT"
    echo "==> $OUTPUT is up to date ($servers server(s))."
else
    # Contains credentials: write owner-only to a temp file, then swap it in atomically.
    tmp="$(mktemp "$OUTPUT.XXXXXX")"
    trap 'rm -f "$tmp"' EXIT
    printf '%s\n' "$config" > "$tmp"
    chmod 600 "$tmp"
    action="Created"
    [[ -e "$OUTPUT" ]] && action="Updated"
    mv -f "$tmp" "$OUTPUT"
    trap - EXIT
    echo "==> $action $OUTPUT ($servers server(s))."
fi
echo "    Set SANDBOX_MCP_CONFIG_FILE=development-sandbox/mcp.json in development-sandbox/.env"
