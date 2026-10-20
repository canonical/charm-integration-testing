# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import pytest
from kubernetes_client import KubernetesClientRegistry


def require_kubeconfig(registry: KubernetesClientRegistry, cloud: str) -> None:
    """Skip before mutation when a test lacks direct access to a Kubernetes cloud."""
    if not registry.has_cloud(cloud):
        variable = f"KUBECONFIG_{cloud.replace('-', '_')}"
        pytest.skip(
            f"Direct Kubernetes access is not configured for cloud '{cloud}'. "
            f"Set {variable} to a kubeconfig file path before starting pytest."
        )
