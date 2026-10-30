#!/bin/bash
#MISE description="Set up the sandbox Kubernetes substrate"
#MISE raw_args=true
#MISE alias="sandbox:setup-k8s"
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

die() {
    echo "ERROR: $*" >&2
    exit 1
}

command -v helm >/dev/null 2>&1 || die "Run this through 'mise run sandbox:setup-k8s' to install Helm."
command -v jq >/dev/null 2>&1 || die "Run this through 'mise run sandbox:setup-k8s' to install jq."

if ! command -v curl >/dev/null 2>&1 \
    || [[ ! -s /etc/ssl/certs/ca-certificates.crt ]]; then
    echo "==> Installing curl and CA certificates..."
    sudo -n apt-get update
    sudo -n apt-get install -y curl ca-certificates
fi

helm_version=$(helm version --short)
[[ "$helm_version" == v3.* ]] || die "Expected Helm 3; found $helm_version."

# ---------------------------------------------------------------------------
# Install Canonical k8s snap
# ---------------------------------------------------------------------------
if snap list k8s &>/dev/null; then
    echo "==> k8s snap already installed."
else
    echo "==> Installing k8s snap (1.32-classic/stable)..."
    sudo snap install --classic --channel=1.32-classic/stable k8s
fi

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
# Install Juju snap
# ---------------------------------------------------------------------------
if snap list juju &>/dev/null; then
    echo "==> juju snap already installed."
else
    echo "==> Installing juju snap..."
    sudo snap install --channel=3/stable juju
fi

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
