#!/bin/bash
#MISE description="Set up the sandbox LXD substrate"
#MISE raw_args=true
#MISE alias="sandbox:setup-lxd"
# Set up LXD and register it as a Juju cloud.
#
# Run this inside the sandbox VM before bootstrapping a Juju controller.
# This script is idempotent: each step checks whether it has already been done.
#
# Usage (from the project root inside the VM):
#   mise run sandbox:setup-lxd
#
# After this script completes, bootstrap a controller:
#   juju bootstrap localhost <controller-name> --bootstrap-constraints root-disk=5G
#   juju add-model <model-name>

set -euo pipefail

# ---------------------------------------------------------------------------
# Install LXD snap
# ---------------------------------------------------------------------------
if snap list lxd &>/dev/null; then
    echo "==> lxd snap already installed."
else
    echo "==> Installing lxd snap..."
    sudo snap install lxd
fi

# ---------------------------------------------------------------------------
# Initialize LXD
# ---------------------------------------------------------------------------
if sudo lxc storage list 2>/dev/null | grep -q "default"; then
    echo "==> LXD already initialized."
else
    echo "==> Initializing LXD..."
    sudo lxd init --auto
fi

# Add ubuntu user to the lxd group if not already a member.
if ! groups ubuntu | grep -q '\blxd\b'; then
    echo "==> Adding ubuntu user to lxd group..."
    sudo usermod -aG lxd ubuntu
    echo "==> Note: group membership takes effect in new shells."
fi

# ---------------------------------------------------------------------------
# Install Juju snap
# ---------------------------------------------------------------------------
if snap list juju &>/dev/null; then
    echo "==> juju snap already installed."
else
    echo "==> Installing juju snap..."
    sudo snap install --channel=3/stable juju
fi

echo ""
echo "LXD substrate ready."
echo "  Next steps:"
echo "    juju bootstrap localhost <controller-name> --bootstrap-constraints root-disk=5G"
echo "    juju add-model <model-name>"
