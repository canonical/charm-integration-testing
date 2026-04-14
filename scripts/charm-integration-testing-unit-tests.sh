#!/bin/bash
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

cd "$(dirname "$0")/.."
poetry run -- pytest "./charm_integration_testing/tests/unit" \
    --cov=./charm_integration_testing/extensions \
    --cov=./charm_integration_testing/juju \
    --cov=./charm_integration_testing/juju_cmd \
    --cov=./charm_integration_testing/juju_jubilant \
    --cov=./charm_integration_testing/kubernetes_client \
    --cov=./charm_integration_testing/serializeable_dataclass \
    --cov=./charm_integration_testing/test_suite \
    --cov-report=term \
    "$@"