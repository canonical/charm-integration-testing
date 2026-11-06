#!/bin/bash
# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.

die () {
    echo -en '\e[1;31m'  # Red, bolded
    echo -n $*
    echo -e '\e[0m'      # Reset coloring
    exit 1
}

cd "$(dirname "$0")/.."
set -e

# Check for prerequisites as appropriate.
which markdownlint-cli2 || die 'markdownlint-cli2 missing; install via "npm install markdownlint-cli2"'

# Actual lint checks begin here.
uv run --no-sync --all-packages ruff check || die 'Failed "ruff check"'
uv run --no-sync --all-packages ruff format --check \
    || die 'Failed on "ruff format --check"; run: uv run --no-sync --all-packages ruff format'

_bandit_paths=(bundle_builder_x charm_integration_testing validators/*)
uv run --no-sync --all-packages bandit \
    --configfile pyproject.toml \
    --quiet \
    --recursive \
    --exclude '*/tests/*' \
    "${_bandit_paths[@]}" \
    scripts \
    || die 'Failed on "bandit"'
uv run --no-sync --all-packages mypy bundle_builder_x || die 'Failed on "mypy bundle_builder_x"'
uv run --no-sync --all-packages mypy charm_integration_testing || die 'Failed on "mypy charm_integration_testing"'
uv run --no-sync --all-packages mypy --explicit-package-bases validators/* || die 'Failed on "mypy validators"'
mapfile -d '' -t yaml_files < <(find . -type d -name .mise -prune -o \
    -type f \( -name '*.yaml' -o -name '*.yml' \) -print0)
if ((${#yaml_files[@]})); then
    uv run --no-sync --all-packages yamlfix --check "${yaml_files[@]}" \
        || die 'Failed on "yamlfix"; consider running: uv run yamlfix'
fi
markdownlint-cli2 --config docs/.sphinx/.markdownlint.json "#docs/_build" "*.md" || die 'Failed on markdownlint-cli2'

echo "Linting passed"
