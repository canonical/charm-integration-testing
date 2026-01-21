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
poetry run ruff check || die 'Failed "ruff check"'
poetry run ruff format --check || die 'Failed on "ruff format --check"; consider running "poetry run ruff format"'
poetry run bandit \
    --configfile pyproject.toml \
    --quiet \
    --recursive \
        charm_integration_testing/extensions \
        charm_integration_testing/juju \
        charm_integration_testing/juju_cmd \
        charm_integration_testing/juju_jubilant \
        charm_integration_testing/serializeable_dataclass \
        charm_integration_testing/test_suite \
        bundle_builder/bundle_builder \
        scripts \
    || die 'Failed on "bandit"'
poetry run mypy bundle_builder || die 'Failed on "mypy bundle_builder"'
poetry run mypy charm_integration_testing || die 'Failed on "mypy charm_integration_testing"'
poetry run yamlfix --check $(find . -name '*.yaml' -o -name '*.yml') || die 'Failed on "yamlfix"'
markdownlint-cli2 --config docs/.sphinx/.markdownlint.json "#docs/_build" "*.md" || die 'Failed on markdownlint-cli2'

echo "Linting passed"
