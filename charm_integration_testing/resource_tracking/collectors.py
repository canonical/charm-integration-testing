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

    Every observed snapshot is recorded uniformly; per-charm opt-outs are not
    applied here.  Skips are resolved once and excluded at diff time in
    :func:`~resource_tracking.discrepancy.calculate_discrepancies`, so a
    per-visit resolution difference cannot make a skipped kind read as drift.
    """

    def __init__(
        self,
        kubernetes_client: KubernetesClient,
        resource_registry: ResourceRegistry,
        sources: Sequence[KubernetesResourceSource] | None = None,
    ) -> None:
        self._kubernetes_client = kubernetes_client
        self._resource_registry = resource_registry
        self._sources: tuple[KubernetesResourceSource, ...] = tuple(sources) if sources is not None else (PvcSource(),)

    def collect(self, logger: logging.Logger) -> list[CollectedResources]:
        collected: list[CollectedResources] = []
        for handle in self._resource_registry.registered_handles():
            if not isinstance(handle, JujuModelHandle):
                continue
            # A model without a namespace is not backed by this cluster (e.g. a
            # machine model); skip it up front rather than issuing a failing list
            # call for every configured source. A transient probe failure is also
            # skipped rather than raising, honouring the best-effort contract.
            try:
                namespace_exists = self._kubernetes_client.namespace_exists(handle.model)
            except ApiException as exc:
                logger.debug("Skipping model '%s' (namespace probe failed)", handle.model, exc_info=exc)
                continue
            if not namespace_exists:
                logger.debug("Skipping non-Kubernetes model '%s'", handle.model)
                continue
            # A partial snapshot is indistinguishable from a genuine absence once
            # recorded, so a single failing source would later be diffed as drift.
            # Drop the whole model observation instead of recording partial data.
            snapshots: set[ResourceSnapshot] = set()
            for source in self._sources:
                try:
                    snapshots.update(source.collect(self._kubernetes_client, handle.model))
                except ApiException as exc:
                    logger.debug("Skipping model '%s' (resource snapshot failed)", handle.model, exc_info=exc)
                    break
            else:
                collected.append(CollectedResources(model=handle.model, snapshots=frozenset(snapshots)))
        return collected
