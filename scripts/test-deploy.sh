#!/bin/bash
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

cd "$(dirname "$0")/.."
poetry run pytest \
    charm_integration_testing/test_suite/deploy \
    --minio-client-file ./static/mc \
    --override-ini junit_suite_name=test-deploy \
    "$@"
