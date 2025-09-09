#!/bin/bash
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

cd "$(dirname "$0")/.."
poetry run -- pytest "./bundle_builder/tests/logic" \
    --cov=./bundle_builder/bundle_builder \
    --cov-report=term \
    --cov-fail-under=80 \
    "$@"
