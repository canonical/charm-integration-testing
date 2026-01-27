#!/bin/bash
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

cd "$(dirname "$0")/.."
poetry run pytest charm_integration_testing/test_suite/teardown --override-ini junit_suite_name=test-teardown "$@"