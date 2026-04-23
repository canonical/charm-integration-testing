#!/bin/bash
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

cd "$(dirname "$0")/.."
poetry run -- pytest "./bundle_builder_x/tests/$1" "${@:2}"
