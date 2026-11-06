#!/bin/bash
# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.

cd "$(dirname "$0")/.."
uv run --no-sync --all-packages ruff check --fix
uv run --no-sync --all-packages ruff format
mapfile -d '' -t yaml_files < <(find . -type d \( -name .mise -o -name .venv \) -prune -o \
    -type f \( -name '*.yaml' -o -name '*.yml' \) -print0)
if ((${#yaml_files[@]})); then
    uv run --no-sync --all-packages yamlfix "${yaml_files[@]}"
fi
markdownlint-cli2 --fix --config docs/.sphinx/.markdownlint.json "#docs/_build" "*.md"
