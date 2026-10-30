#!/bin/bash
#MISE description="Run charm integration suite tests"
#MISE raw_args=true
#MISE alias="test:suite"
#MISE env.GIT_CONFIG_GLOBAL="{{ env.GIT_CONFIG_GLOBAL | default(value='/dev/null') }}"
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

cd "$(dirname "$0")/.."
poetry run pytest \
    charm_integration_testing/test_suite \
    "$@"
