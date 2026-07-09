# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Kubernetes sources that collect resource snapshots from a live cluster.

Each source knows how to turn one kind of raw Kubernetes object into
:class:`~resource_tracking.snapshot.ResourceSnapshot` values.  Keeping the
mapping here -- rather than in ``kubernetes_client`` -- lets the Kubernetes
facade stay in the business of returning raw API objects (like ``get_charm_pods``
does) while all snapshot construction lives alongside the collector that consumes
it.  These sources are Kubernetes-specific; other substrates (LXD, OpenStack)
would supply their own collector rather than reusing this interface.  New
Kubernetes resource kinds are supported by adding another source.
"""

from __future__ import annotations

from typing import Protocol

from kubernetes_client import KubernetesClient

from .snapshot import PvcSnapshot, ResourceSnapshot


class KubernetesResourceSource(Protocol):
    """Collects snapshots of a single Kubernetes resource kind for one model."""

    def collect(self, kubernetes_client: KubernetesClient, model: str) -> list[ResourceSnapshot]:
        """Return snapshots for this resource kind in ``model``'s namespace.

        Raises:
            kubernetes.client.ApiException: If the cluster query fails.
        """


class PvcSource:
    """Collects :class:`PvcSnapshot` values from a model's namespace."""

    def collect(self, kubernetes_client: KubernetesClient, model: str) -> list[ResourceSnapshot]:
        pvcs = kubernetes_client.list_model_pvcs(model=model)
        return [
            PvcSnapshot(
                name=pvc.metadata.name,
                namespace=model,
                storage_class=pvc.spec.storage_class_name or "",
                requested_storage=(pvc.spec.resources.requests or {}).get("storage", ""),
                phase=pvc.status.phase or "",
                application=(pvc.metadata.labels or {}).get("app.kubernetes.io/name", ""),
            )
            for pvc in pvcs
        ]
