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
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from juju.backend import JujuBackend
from juju.resource_registry import JujuModelHandle
from kubernetes.client import ApiException  # type: ignore[import-untyped]
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
    with an OpenStack neighbor).  Rather than resolving a client per controller
    up front, ``collect`` asks ``juju_backend`` for a client for each model's
    controller as it goes, so every registered model is queried against the
    cluster it actually lives on rather than always the target's.  A controller
    that is not Kubernetes-based (or whose client cannot be resolved) simply
    contributes no snapshots.

    Resources can be excluded per application via ``resource_skips``: a mapping
    of application name to the resource types that application opts out of.  A
    snapshot is dropped when its owning application skips its resource type, so
    the same model can still track that resource type for other applications. 
    Every observed snapshot is recorded uniformly; per-charm opt-outs are not
    applied here.  Skips are resolved once and excluded at diff time in
    :func:`~resource_tracking.discrepancy.calculate_discrepancies`, so a
    per-visit resolution difference cannot make a skipped kind read as drift.
    """

    def __init__(
        self,
        juju_backend: JujuBackend,
        resource_registry: ResourceRegistry,
        sources: Sequence[KubernetesResourceSource] | None = None,
    ) -> None:
        self._juju_backend = juju_backend
        self._resource_registry = resource_registry
        self._sources: tuple[KubernetesResourceSource, ...] = tuple(sources) if sources is not None else (PvcSource(),)

    def collect(self, logger: logging.Logger) -> list[CollectedResources]:
        collected: list[CollectedResources] = []
        for handle in self._resource_registry.registered_handles():
            if not isinstance(handle, JujuModelHandle):
                continue
            try:
                kubernetes_client = self._juju_backend.get_kubernetes_client_for_controller(handle.controller)
            except Exception:
                logger.debug(
                    "Could not resolve Kubernetes client for controller '%s'.", handle.controller, exc_info=True
                )
                continue
            if kubernetes_client is None:
                # The model's controller is not Kubernetes-based - nothing to collect there.
                continue
            snapshots: set[ResourceSnapshot] = set()
            for source in self._sources:
                try:
                    snapshots.update(source.collect(kubernetes_client, handle.model))
                except ApiException as exc:
                    logger.debug("Skipping model '%s' (resource snapshot failed)", handle.model, exc_info=exc)
                    break
            else:
                collected.append(CollectedResources(model=handle.model, snapshots=frozenset(snapshots)))
        return collected
