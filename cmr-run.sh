#!/usr/bin/env bash

set -euo pipefail

export MINIO_CLIENT_FILE="$PWD/static/mc"
export UV_FILE="$PWD/static/uv"
export VALIDATORS_PATH="$PWD/validators"

export BUNDLE_OUTPUT="./k8s-generated-bundle.yaml"
export BUNDLE_MERMAID_OUTPUT="./k8s-generated-bundle.mmd"

export model="model"
# export controller="lxd"
# export cloud="localhost"
export cloud="k8s"
export controller="k8s"
export KUBECONFIG="$HOME/.kube/config"

export charm_under_test="indico"
export revision="default"
export channel="default"
export series="default"

export neighbor="postgresql"

export charm_endpoint="database"
export neighbor_endpoint="database"

export platform="kubernetes"
export log_level="INFO"

./scripts/run-tests.sh \
    --target-charm "${charm_under_test}" \
    --target-channel "${channel}" \
    --target-revision "${revision}" \
    --target-series "${series}" \
    --neighbor-charm "${neighbor}" \
    --model "${model}" \
    --juju-cloud "${cloud}" \
    --juju-controller "${controller}" \
    \
    --bundle "./static/bundles/cmr-demo-1/postgresql.yaml:localhost:lxd:model" \
    --bundle "./static/bundles/cmr-demo-1/indico.yaml:k8s:k8s:model" \
    \
    --current-state "no_controller" \
    -k 'not test_build_bundle and not test_remove_and_restore_integration' \
    \
    --mermaid-output "${BUNDLE_MERMAID_OUTPUT}" \
    --target-application "target" \
    --target-endpoint "${charm_endpoint}" \
    --neighbor-application "neighbor" \
    --neighbor-endpoint "${neighbor_endpoint}" \
    --platform "${platform}" \
    --charm-metadata-overrides "./static/charm-metadata-overrides/" \
    --charm-platform-overrides "./static/charm-platform-overrides/" \
    --charm-listing-overrides "./static/charm-listing-overrides.yaml" \
    --charm-test-configs "./static/charm-test-configs/" \
    --charm-priorities-config "./static/charm-priorities.yaml" \
    --charm-default-versions "./static/charm-default-versions.yaml" \
    --juju-model-config "./static/juju-model-config.json" \
    --juju-controller-bootstrap-constraints "./static/juju-bootstrap-constraints.json" \
    --log-cli-level "${log_level}" \
    --log-level "${log_level}" \
    --junit-xml=junit.xml
