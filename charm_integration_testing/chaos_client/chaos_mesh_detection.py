# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from kubernetes_client import KubernetesBackend

CHAOS_MESH_CRDS = ("stresschaos.chaos-mesh.org", "iochaos.chaos-mesh.org")


def missing_chaos_mesh_crds(backend: KubernetesBackend) -> list[str]:
    """Return missing CRDs, propagating API errors."""
    # Check every CRD so an absent one does not hide an API error for another.
    return [name for name in CHAOS_MESH_CRDS if not backend.crd_exists(name)]


def chaos_mesh_is_available(backend: KubernetesBackend) -> bool:
    """Check whether StressChaos or IOChaos experiments are available."""
    return len(missing_chaos_mesh_crds(backend)) < len(CHAOS_MESH_CRDS)
