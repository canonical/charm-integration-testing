#!/bin/bash
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

cd "$(dirname "$0")/.."
poetry run bundle-builder-v2 \
    --charm-scriptlet-overrides ./static/charm-scriptlet-overrides/ \
    --charm-platform-overrides ./static/charm-platform-overrides/ \
    --charm-listing-overrides ./static/charm-listing-overrides.yaml \
    --charm-priorities ./static/charm-priorities.yaml \
    "$@"
