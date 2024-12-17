#!/bin/bash
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

cd "$(dirname "$0")/.."
poetry run pytest charm_integration_testing/test_suites "$@"
