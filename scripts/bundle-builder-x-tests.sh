#!/bin/bash
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

cd "$(dirname "$0")/.."

IFS='|' read -ra TYPES <<< "$1"
PATHS=()
for t in "${TYPES[@]}"; do
    PATHS+=("./bundle_builder_x/tests/$t")
done

poetry run -- pytest "${PATHS[@]}" "${@:2}"
