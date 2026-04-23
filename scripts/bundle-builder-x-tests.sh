#!/bin/bash
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

cd "$(dirname "$0")/.."

if [[ -z "$1" ]]; then
    echo "Usage: $0 <type>[|<type>...] [pytest-args...]"
    echo "Types: unit, logic, integration"
    echo "Example: $0 'unit|logic' -q"
    exit 1
fi

IFS='|' read -ra TYPES <<< "$1"
PATHS=()
for t in "${TYPES[@]}"; do
    PATHS+=("./bundle_builder_x/tests/$t")
done

poetry run -- pytest "${PATHS[@]}" "${@:2}"
