#!/bin/bash
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

cd "$(dirname "$0")/.."
poetry run bundle-builder-v5 \
    --charm-metadata-overrides ./static/charm-metadata-overrides-v5/ \
    --charm-platform-overrides ./static/charm-platform-overrides/ \
    --charm-listing-overrides ./static/charm-listing-overrides.yaml \
    --charm-test-configs ./static/charm-test-configs-v5/ \
    --charm-priorities ./static/charm-priorities.yaml \
    "$@"
