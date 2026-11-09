#!/bin/bash
# Source this file to define write_sandbox_env_file, then call:
#
#   write_sandbox_env_file ENV_FILE MCP_CONFIG_FILE
#
# Writes the host values of the variables scripts/sandbox.sh reads from .env,
# plus MCP credentials as SANDBOX_VAR_<NAME> so they are also passed into the
# VM, and SANDBOX_MCP_CONFIG_FILE=MCP_CONFIG_FILE. If ENV_FILE already exists
# and differs, lists the variable names (never values) that would be removed
# or added and asks before overwriting it.

# shellcheck source-path=SCRIPTDIR source=generate-mcp-json.sh
source "$(dirname "${BASH_SOURCE[0]}")/generate-mcp-json.sh"

# Print the sorted variable names assigned in an env file (never the values).
_env_names() {
    local line
    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$line" =~ ^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)= ]]; then
            printf '%s\n' "${BASH_REMATCH[2]}"
        fi
    done < "$1" | sort -u
}

# Global so the EXIT trap can still see it if the caller fails mid-function.
_env_tmp=""

write_sandbox_env_file() {
    local env_file="$1" mcp_config_file="$2"
    local -A vars=()
    local -a missing excess
    local name key answer

    for name in SANDBOX_VM SANDBOX_MOUNT SANDBOX_CPUS SANDBOX_MEMORY SANDBOX_DISK GITHUB_TOKEN; do
        if [[ -n "${!name:-}" ]]; then vars[$name]="${!name}"; fi
    done
    while IFS= read -r name; do
        if [[ "$name" == SANDBOX_VAR_?* ]]; then vars[$name]="${!name}"; fi
    done < <(compgen -e)
    while IFS= read -r key; do
        if [[ -n "${!key:-}" ]]; then vars[SANDBOX_VAR_$key]="${!key}"; fi
    done < <(mcp_env_keys)
    vars[SANDBOX_MCP_CONFIG_FILE]="$mcp_config_file"

    # Contains credentials: build owner-only in a temp file, then swap it in.
    _env_tmp="$(mktemp "$env_file.XXXXXX")"
    trap 'rm -f "$_env_tmp"' EXIT
    chmod 600 "$_env_tmp"
    while IFS= read -r key; do
        printf '%s=%q\n' "$key" "${vars[$key]}"
    done < <(printf '%s\n' "${!vars[@]}" | sort) > "$_env_tmp"

    if [[ -f "$env_file" ]]; then
        if cmp -s "$_env_tmp" "$env_file"; then
            echo "==> $env_file is up to date."
            rm -f "$_env_tmp"
            trap - EXIT
            return 0
        fi

        mapfile -t missing < <(comm -23 <(_env_names "$env_file") <(_env_names "$_env_tmp"))
        mapfile -t excess < <(comm -13 <(_env_names "$env_file") <(_env_names "$_env_tmp"))
        {
            echo "WARNING: $env_file already exists and differs from your environment."
            if [[ ${#missing[@]} -gt 0 ]]; then
                echo "  Missing from your environment (removed if you overwrite):"
                printf '    - %s\n' "${missing[@]}"
            fi
            if [[ ${#excess[@]} -gt 0 ]]; then
                echo "  Excess in your environment (added if you overwrite):"
                printf '    - %s\n' "${excess[@]}"
            fi
            if [[ ${#missing[@]} -eq 0 && ${#excess[@]} -eq 0 ]]; then
                echo "  Same variable names; only values or formatting differ."
            fi
        } >&2

        answer=""
        if [[ -t 0 ]]; then
            read -r -p "Overwrite $env_file? [y/N] " answer
        else
            echo "No terminal to confirm." >&2
        fi
        if [[ ! "$answer" =~ ^[Yy]([Ee][Ss])?$ ]]; then
            echo "==> Keeping the existing $env_file."
            rm -f "$_env_tmp"
            trap - EXIT
            return 0
        fi
    fi

    mv -f "$_env_tmp" "$env_file"
    trap - EXIT
    echo "==> Wrote $env_file with:"
    printf '%s\n' "${!vars[@]}" | sort | sed 's/^/      /'
}
