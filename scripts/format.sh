#!/bin/bash
# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.

cd "$(dirname "$0")/.."
poetry run ruff check --fix
poetry run ruff format
mapfile -d '' -t yaml_files < <(find . -type d -name .mise -prune -o \
    -type f \( -name "*.yaml" -o -name "*.yml" \) -print0)
poetry run yamlfix "${yaml_files[@]}"
markdownlint-cli2 --fix --config docs/.sphinx/.markdownlint.json "#docs/_build" "*.md"
