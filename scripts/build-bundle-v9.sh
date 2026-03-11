#!/bin/bash
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

cd "$(dirname "$0")/.."
poetry run bundle-builder-v9 \
    --charm-overrides ./static/charm-overrides/ \
    "$@"
