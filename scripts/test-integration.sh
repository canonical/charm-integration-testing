#!/bin/bash
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

cd "$(dirname "$0")/.."
set +e
poetry run pytest charm_integration_testing/test_suite/integration --override-ini junit_suite_name=test-integration "$@"
exit_code=$?
set -e

# Pytest exit codes:
# 0 - All tests passed
# 1 - Tests were collected and run but some failed
# 2 - Test execution was interrupted by the user
# 3 - Internal error happened while executing tests
# 4 - pytest command line usage error
# 5 - No tests were collected
exit $exit_code
