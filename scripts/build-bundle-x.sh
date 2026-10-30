#!/bin/bash
#MISE description="Build a bundle-builder-x bundle"
#MISE raw_args=true
#MISE alias="bundle:build-x"
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

cd "$(dirname "$0")/.."
poetry run bundle-builder-x \
    --overrides ./static/charm-overrides/ \
    "$@"
