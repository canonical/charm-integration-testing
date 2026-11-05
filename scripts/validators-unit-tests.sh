#!/bin/bash
#MISE description="Run validator unit tests"
#MISE raw_args=true
#MISE alias="test:validators"
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
