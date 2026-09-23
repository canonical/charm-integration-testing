# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from kubernetes_client import KubernetesBackend

CHAOS_MESH_CRDS = ("stresschaos.chaos-mesh.org", "iochaos.chaos-mesh.org")


def chaos_mesh_is_available(backend: KubernetesBackend) -> bool:
    """Check whether the StressChaos and IOChaos CRDs are present."""
    # Check both CRDs so an absent one does not hide an API error for the other.
    present = [backend.crd_exists(name) for name in CHAOS_MESH_CRDS]
    return all(present)
