#!/bin/bash
#MISE description="Set up the sandbox Kubernetes substrate"
#MISE raw_args=true
#MISE alias="sandbox:setup-k8s"
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
# Set up Canonical k8s and register it as a Juju cloud.
#
# Run this inside the sandbox VM before bootstrapping a Juju controller.
# This script is idempotent: each step checks whether it has already been done.
#
# Usage (from the project root inside the VM):
#   mise run sandbox:setup-k8s
#
# After this script completes, register the cloud and bootstrap a controller:
#   juju add-k8s local-k8s --client
#   juju bootstrap local-k8s <controller-name> --bootstrap-constraints root-disk=5G
#   juju add-model <model-name>

set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/_require-sandbox.sh"

die() {
    echo "ERROR: $*" >&2
    exit 1
}

for tool in helm jq k8s kubectl juju; do
    command -v "$tool" >/dev/null ||
        die "Missing $tool; run: mise run setup -- --group local-k8s"
done

helm_version=$(helm version --short)
[[ "$helm_version" == v3.* ]] || die "Expected Helm 3; found $helm_version."

# ---------------------------------------------------------------------------
# Bootstrap k8s
# ---------------------------------------------------------------------------
if sudo k8s status 2>/dev/null | grep -q "cluster status: ready"; then
    echo "==> k8s cluster already ready."
else
    echo "==> Bootstrapping Canonical k8s..."
    sudo k8s bootstrap
    echo "==> Waiting for k8s to be ready (up to 10 min)..."
    sudo k8s status --wait-ready --timeout 10m
fi

# ---------------------------------------------------------------------------
# Enable local-storage addon
# ---------------------------------------------------------------------------
if sudo k8s kubectl get storageclass 2>/dev/null | grep -q "local-storage"; then
    echo "==> local-storage addon already enabled."
else
    echo "==> Enabling local-storage addon..."
    sudo k8s enable local-storage
fi

# ---------------------------------------------------------------------------
# Write kubeconfig
# ---------------------------------------------------------------------------
echo "==> Writing kubeconfig..."
sudo k8s config | tee /home/ubuntu/k8s.yaml > /dev/null
chown ubuntu:ubuntu /home/ubuntu/k8s.yaml || true

# ---------------------------------------------------------------------------
# Register k8s cloud with Juju
# ---------------------------------------------------------------------------
if juju clouds --client 2>/dev/null | grep -q "local-k8s"; then
    echo "==> local-k8s cloud already registered."
else
    echo "==> Registering local-k8s cloud with Juju..."
    KUBECONFIG=/home/ubuntu/k8s.yaml juju add-k8s local-k8s --client
fi

# ---------------------------------------------------------------------------
# Install or verify Chaos Mesh
# ---------------------------------------------------------------------------
echo "==> Setting up Chaos Mesh..."
bash "$(dirname "${BASH_SOURCE[0]}")/setup-chaos-mesh.sh" /home/ubuntu/k8s.yaml

echo ""
echo "k8s substrate ready."
echo "  Next steps:"
echo "    juju bootstrap local-k8s <controller-name> --bootstrap-constraints root-disk=5G"
echo "    juju add-model <model-name>"
echo ""
echo "  The kubeconfig is at /home/ubuntu/k8s.yaml"
echo "  Set KUBECONFIG=/home/ubuntu/k8s.yaml if juju commands need it."
