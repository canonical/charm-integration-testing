#!/bin/bash
#MISE description="Run charm integration suite tests"
#MISE raw_args=true
#MISE alias="test:suite"
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

cd "$(dirname "$0")/.."
poetry run pytest \
    charm_integration_testing/test_suite \
    "$@"
