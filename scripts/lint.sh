#!/bin/bash
# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.

cd "$(dirname "$0")/.."
set -e
poetry run ruff check
poetry run ruff format --check
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
        scripts
poetry run mypy bundle_builder
poetry run mypy charm_integration_testing
poetry run yamllint -d relaxed --no-warnings .
markdownlint-cli2 --config docs/.sphinx/.markdownlint.json "#docs/_build" "*.md"
