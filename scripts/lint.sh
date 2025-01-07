#!/bin/bash
# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.

cd "$(dirname "$0")/.."
set -e
poetry run ruff check
poetry run ruff format --check
poetry run bandit --configfile pyproject.toml --quiet --recursive test_scheduler scripts
poetry run mdformat --check --wrap 100 .
