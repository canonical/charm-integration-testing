#!/bin/bash
# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.

cd "$(dirname "$0")/.."
poetry run ruff check --fix
poetry run ruff format
poetry run mdformat --wrap 100 .
