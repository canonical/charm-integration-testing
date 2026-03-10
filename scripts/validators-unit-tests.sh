#!/bin/bash
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

cd "$(dirname "$0")/.."

cov_args=()
for dir in ./validators/*/; do
    cov_args+=(--cov="${dir%/}")
done

poetry run -- pytest \
    ./validators/*/tests/unit \
    "${cov_args[@]}" \
    --cov-report=term \
