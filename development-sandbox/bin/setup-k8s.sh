#!/bin/bash
# Set up Canonical k8s and register it as a Juju cloud.
#
# Run this inside the sandbox VM before bootstrapping a Juju controller.
# This script is idempotent: each step checks whether it has already been done.
#
# Usage (from inside the VM):
#   /project/development-sandbox/bin/setup-k8s.sh
#
# After this script completes, register the cloud and bootstrap a controller:
#   juju add-k8s local-k8s --client
#   juju bootstrap local-k8s <controller-name>
#   juju add-model <model-name>

set -euo pipefail

HELM_VERSION="v3.21.4"
HELM_INSTALL_PATH="/usr/local/bin/helm"

die() {
    echo "ERROR: $*" >&2
    exit 1
}

# Check existing Helm before making environment changes.
if command -v helm >/dev/null 2>&1; then
    helm_version=$(helm version --short)
    [[ "$helm_version" == v3.* ]] \
        || die "Expected Helm 3; found $helm_version. Existing installation was not changed."
    echo "==> Using existing Helm: $helm_version"
else
    [[ ! -e "$HELM_INSTALL_PATH" && ! -L "$HELM_INSTALL_PATH" ]] \
        || die "$HELM_INSTALL_PATH already exists but is not available in PATH."
fi

# ---------------------------------------------------------------------------
# Prepare tools required for Chaos Mesh installation
# ---------------------------------------------------------------------------
if ! command -v curl >/dev/null 2>&1 \
    || ! command -v jq >/dev/null 2>&1 \
    || [[ ! -s /etc/ssl/certs/ca-certificates.crt ]]; then
    echo "==> Installing curl, jq, and CA certificates..."
    sudo -n apt-get update
    sudo -n apt-get install -y curl jq ca-certificates
fi

if ! command -v helm >/dev/null 2>&1; then
    case "$(uname -m)" in
        x86_64) helm_arch="amd64" ;;
        aarch64|arm64) helm_arch="arm64" ;;
        *) die "Unsupported architecture for the Helm installer: $(uname -m)" ;;
    esac

    echo "==> Installing Helm $HELM_VERSION ($helm_arch)..."
    helm_archive="helm-${HELM_VERSION}-linux-${helm_arch}.tar.gz"
    helm_tmp=$(mktemp -d)
    trap 'rm -rf -- "$helm_tmp"' EXIT

    curl -fsSL "https://get.helm.sh/${helm_archive}" \
        -o "${helm_tmp}/${helm_archive}"
    curl -fsSL "https://get.helm.sh/${helm_archive}.sha256sum" \
        -o "${helm_tmp}/${helm_archive}.sha256sum"

    pushd "$helm_tmp" >/dev/null
    sha256sum --check "${helm_archive}.sha256sum"
    tar -xzf "$helm_archive" "linux-${helm_arch}/helm"

    downloaded_version=$("./linux-${helm_arch}/helm" version --short)
    [[ "$downloaded_version" == "${HELM_VERSION}"+* ]] \
        || die "Downloaded Helm reports unexpected version: $downloaded_version"

    sudo -n install -m 0755 "linux-${helm_arch}/helm" "$HELM_INSTALL_PATH"
    popd >/dev/null

    rm -rf -- "$helm_tmp"
    trap - EXIT
    hash -r

    command -v helm >/dev/null 2>&1 \
        || die "Helm was installed to $HELM_INSTALL_PATH; add /usr/local/bin to PATH."
    helm version --short
fi

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
echo "    juju bootstrap local-k8s <controller-name>"
echo "    juju add-model <model-name>"
echo ""
echo "  The kubeconfig is at /home/ubuntu/k8s.yaml"
echo "  Set KUBECONFIG=/home/ubuntu/k8s.yaml if juju commands need it."
