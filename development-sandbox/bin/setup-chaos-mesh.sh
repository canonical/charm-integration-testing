#!/bin/bash
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Install Chaos Mesh on the sandbox after Kubernetes setup.
# Usage: bash setup-chaos-mesh.sh [kubeconfig]
# Requires Helm 3, jq, and kubectl or Canonical k8s.

set -euo pipefail

CHART_VERSION="2.8.4"
CHART_REPOSITORY="https://charts.chaos-mesh.org"
RELEASE="chaos-mesh"
NAMESPACE="chaos-mesh"
SOCKET_PATH="/run/containerd/containerd.sock"
INSTALL_TIMEOUT="10m"
CRD_TIMEOUT="120s"
KUBECONFIG_PATH="${1:-/home/ubuntu/k8s.yaml}"

die() {
    echo "ERROR: $*" >&2
    exit 1
}

[[ $# -le 1 ]] || die "Usage: $0 [kubeconfig]"
[[ -f "$KUBECONFIG_PATH" && -r "$KUBECONFIG_PATH" ]] \
    || die "Kubeconfig must be a readable file: $KUBECONFIG_PATH"

command -v helm >/dev/null 2>&1 || die "Install Helm 3 before running this script."
command -v jq >/dev/null 2>&1 || die "jq is required."
helm_version=$(helm version --short)
[[ "$helm_version" == v3.* ]] || die "Expected Helm 3; found $helm_version."

if command -v kubectl >/dev/null 2>&1; then
    kubectl_cmd=(kubectl)
elif command -v k8s >/dev/null 2>&1; then
    kubectl_cmd=(sudo -n k8s kubectl)
else
    die "kubectl or Canonical k8s is required."
fi
kubectl_cmd+=(--kubeconfig "$KUBECONFIG_PATH")
helm_args=(--kubeconfig "$KUBECONFIG_PATH" --namespace "$NAMESPACE")

echo "==> Target cluster nodes:"
"${kubectl_cmd[@]}" get nodes

releases=$(helm list "${helm_args[@]}" --all --filter "^${RELEASE}$" --output json)
release_count=$(jq 'length' <<< "$releases")

if [[ "$release_count" == "0" ]]; then
    echo "==> Installing Chaos Mesh $CHART_VERSION with $helm_version..."
    # Atomic failure cleanup may leave CRDs and the namespace behind.
    helm install "$RELEASE" chaos-mesh \
        --repo "$CHART_REPOSITORY" \
        "${helm_args[@]}" \
        --create-namespace \
        --version "$CHART_VERSION" \
        --set-string chaosDaemon.runtime=containerd \
        --set-string chaosDaemon.socketPath="$SOCKET_PATH" \
        --set dashboard.create=false \
        --atomic \
        --timeout "$INSTALL_TIMEOUT"
elif [[ "$release_count" == "1" ]]; then
    jq -e --arg chart "chaos-mesh-${CHART_VERSION}" \
        '.[0].status == "deployed" and .[0].chart == $chart' \
        <<< "$releases" >/dev/null \
        || die "Existing release has a different version or status; inspect it before proceeding."

    values=$(helm get values "$RELEASE" "${helm_args[@]}" --all --output json)
    jq -e --arg socket "$SOCKET_PATH" '
        .chaosDaemon.runtime == "containerd"
        and .chaosDaemon.socketPath == $socket
        and .dashboard.create == false
    ' <<< "$values" >/dev/null \
        || die "Existing release settings differ; no automatic upgrade will be performed."

    echo "==> Chaos Mesh $CHART_VERSION already installed with the expected settings."
else
    die "Unexpected release listing; inspect it before proceeding."
fi

echo "==> Checking CRDs and component readiness..."
"${kubectl_cmd[@]}" wait \
    --for=condition=Established \
    crd/stresschaos.chaos-mesh.org \
    crd/iochaos.chaos-mesh.org \
    --timeout="$CRD_TIMEOUT"

for deployment in chaos-controller-manager chaos-dns-server; do
    "${kubectl_cmd[@]}" rollout status "deployment/$deployment" \
        --namespace "$NAMESPACE" --timeout="$INSTALL_TIMEOUT"
done
"${kubectl_cmd[@]}" rollout status daemonset/chaos-daemon \
    --namespace "$NAMESPACE" --timeout="$INSTALL_TIMEOUT"

echo "==> Chaos Mesh $CHART_VERSION is ready. No chaos experiments were created."