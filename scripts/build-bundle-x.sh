#!/bin/bash
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

cd "$(dirname "$0")/.."
uv run --no-sync --all-packages bundle-builder-x \
    --overrides ./static/charm-overrides/ \
    "$@"
