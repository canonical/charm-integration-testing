#!/bin/bash
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
# Solutions QA entry point for the development sandbox. Accepts the same
# subcommands as scripts/sandbox.sh. For `up`, it first:
#   - creates development-sandbox/mcp.json from mcp.json.sample if it does not
#     exist (fails before writing if any required variable is unset), and
#   - writes the host values of variables listed in development-sandbox/.env.sample
#     to development-sandbox/.env. If that file already exists, it lists the
#     variable names that would be removed or added and asks before overwriting.
# Usage:
#   JIRA_API_TOKEN=... TEST_OBSERVER_API_KEY=... \
#       scripts/sandbox-solutions-qa.sh up [--cpus N] [--memory SIZE] [--disk SIZE]
#   scripts/sandbox-solutions-qa.sh <down|destroy|shell|run|--help> ...

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEV_DIR="$(cd "$SCRIPT_DIR/../development-sandbox" && pwd)"

# shellcheck source-path=SCRIPTDIR source=util/generate-mcp-json.sh
source "$SCRIPT_DIR/util/generate-mcp-json.sh"
# shellcheck source-path=SCRIPTDIR source=util/sandbox-env.sh
source "$SCRIPT_DIR/util/sandbox-env.sh"

case "${1:-}" in
    up)
        shift
        ensure_mcp_json "$DEV_DIR/mcp.json"
        write_sandbox_env_file "$DEV_DIR/.env"
        exec "$SCRIPT_DIR/sandbox.sh" up "$@"
        ;;
    *)
        exec "$SCRIPT_DIR/sandbox.sh" "$@"
        ;;
esac
