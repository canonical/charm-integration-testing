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
        snapshots: list[ResourceSnapshot] = []
        for pvc in pvcs:
            # ``spec``, ``spec.resources`` and ``status`` are optional on the raw
            # Kubernetes objects and may be ``None``; guard each hop so a partially
            # populated PVC does not abort collection for the whole model.
            spec = pvc.spec
            status = pvc.status
            requests = spec.resources.requests if spec is not None and spec.resources is not None else None
            snapshots.append(
                PvcSnapshot(
                    name=pvc.metadata.name,
                    namespace=model,
                    storage_class=(spec.storage_class_name if spec is not None else None) or "",
                    requested_storage=(requests or {}).get("storage", ""),
                    phase=(status.phase if status is not None else None) or "",
                    application=(pvc.metadata.labels or {}).get("app.kubernetes.io/name", ""),
                )
            )
        return snapshots
