# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Substrate-specific collection of resource snapshots.

A :class:`ResourceCollector` encapsulates *how* to gather resource snapshots
from one substrate.  :class:`KubernetesResourceCollector` is the only
implementation today, but the abstraction lets an ``lxd`` or ``openstack``
collector be added later without touching
:class:`~resource_tracking.tracker.StateResourceTracker`, which records whatever
collectors produce and knows nothing about namespaces or Kubernetes clients.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from juju.resource_registry import JujuModelHandle
from kubernetes.client import ApiException  # type: ignore[import-untyped]
from kubernetes_client import KubernetesClient
from resource_registry import ResourceRegistry

from .snapshot import ResourceSnapshot
from .sources import KubernetesResourceSource, PvcSource


@dataclass(frozen=True)
class CollectedResources:
    """Snapshots collected for a single tracked scope (e.g. a Juju model)."""

    model: str
    snapshots: frozenset[ResourceSnapshot]


class ResourceCollector(Protocol):
    """Collects resource snapshots from one substrate."""

    def collect(self, logger: logging.Logger) -> list[CollectedResources]:
        """Return the resources currently present in every tracked scope.

        Collection is best-effort: a scope that cannot be queried is skipped
        rather than raising, so a partial substrate never fails the suite.
        """


class KubernetesResourceCollector:
    """Collects resource snapshots for every registered Kubernetes model.

    A CMR run can span multiple Juju controllers, each potentially bootstrapped
    on a different cloud (e.g. two Kubernetes clusters, or a Kubernetes target
    with an OpenStack neighbor).  ``kubernetes_clients`` maps each Kubernetes
    controller name to the client for its cloud, so every registered model is
    queried against the cluster it actually lives on rather than always the
    target's.  A controller absent from the mapping (e.g. it is not
    Kubernetes-based) is simply skipped - its models contribute no snapshots.

    Resources can be excluded per application via ``resource_skips``: a mapping
    of application name to the resource types that application opts out of.  A
    snapshot is dropped when its owning application skips its resource type, so
    the same model can still track that resource type for other applications.
    """

    def __init__(
        self,
        kubernetes_clients: Mapping[str, KubernetesClient],
        resource_registry: ResourceRegistry,
        sources: Sequence[KubernetesResourceSource] | None = None,
        resource_skips: Mapping[str, frozenset[str]] | None = None,
    ) -> None:
        self._kubernetes_clients = kubernetes_clients
        self._resource_registry = resource_registry
        self._sources: tuple[KubernetesResourceSource, ...] = tuple(sources) if sources is not None else (PvcSource(),)
        self._resource_skips: Mapping[str, frozenset[str]] = resource_skips if resource_skips is not None else {}

    def collect(self, logger: logging.Logger) -> list[CollectedResources]:
        collected: list[CollectedResources] = []
        for handle in self._resource_registry.registered_handles():
            if not isinstance(handle, JujuModelHandle):
                continue
            kubernetes_client = self._kubernetes_clients.get(handle.controller)
            if kubernetes_client is None:
                # The model's controller is not Kubernetes-based (or its client
                # could not be resolved) - nothing to collect there.
                continue
            snapshots: set[ResourceSnapshot] = set()
            for source in self._sources:
                try:
                    snapshots.update(source.collect(kubernetes_client, handle.model))
                except ApiException as exc:
                    # A model whose namespace does not exist (e.g. a non-Kubernetes
                    # model) is skipped rather than raising.
                    logger.debug("Skipping resource snapshot for model '%s'", handle.model, exc_info=exc)
            tracked = frozenset(snapshot for snapshot in snapshots if not self._is_skipped(snapshot))
            collected.append(CollectedResources(model=handle.model, snapshots=tracked))
        return collected

    def _is_skipped(self, snapshot: ResourceSnapshot) -> bool:
        return snapshot.resource_type in self._resource_skips.get(snapshot.application, frozenset())
