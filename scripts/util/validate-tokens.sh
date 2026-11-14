#!/bin/bash
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Source this file to define validate_sandbox_tokens.
#
# Checks the sandbox GitHub tokens set in the host environment. GitHub cannot
# report a fine-grained token's own permissions, so this checks only what can
# be verified: the two tokens differ, both are fine-grained, and GitHub
# accepts each one.

# Print the HTTP status of GET /user for token $1, or 000 if GitHub is unreachable.
_github_user_status() {
    # Header via stdin so the token never appears in the process list.
    printf 'Authorization: Bearer %s\n' "$1" \
        | curl -sS -o /dev/null -w '%{http_code}' --max-time 10 -H @- \
            -H 'Accept: application/vnd.github+json' https://api.github.com/user 2>/dev/null \
        || true
}

validate_sandbox_tokens() {
    local -a errors=()
    local name token code

    if [[ -n "${SANDBOX_VAR_GITHUB_TOKEN:-}" \
        && "$SANDBOX_VAR_GITHUB_TOKEN" == "${SANDBOX_VAR_COPILOT_GITHUB_TOKEN:-}" ]]; then
        errors+=("SANDBOX_VAR_GITHUB_TOKEN and SANDBOX_VAR_COPILOT_GITHUB_TOKEN are the same token; use a canonical-owned token for gh and a separate user-owned token for Copilot.")
    fi

    for name in SANDBOX_VAR_GITHUB_TOKEN SANDBOX_VAR_COPILOT_GITHUB_TOKEN; do
        token="${!name:-}"
        [[ -n "$token" ]] || continue
        if [[ "$token" != github_pat_* ]]; then
            errors+=("$name is not a fine-grained personal access token (github_pat_...).")
            continue
        fi
        code="$(_github_user_status "$token")"
        case "$code" in
            200) ;;
            401) errors+=("$name was rejected by GitHub (HTTP 401); it may be expired or revoked.") ;;
            000) echo "WARNING: could not reach api.github.com to check $name; skipping that check." >&2 ;;
            *) echo "WARNING: unexpected HTTP $code while checking $name; skipping that check." >&2 ;;
        esac
    done

    if [[ ${#errors[@]} -gt 0 ]]; then
        echo "ERROR: invalid sandbox GitHub tokens:" >&2
        printf '  - %s\n' "${errors[@]}" >&2
        return 1
    fi
}
