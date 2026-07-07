# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Recording of per-state resource observations.

The scheduler drives the environment through a series of canonical
:class:`~test_suite.scheduler.states.State` values.  This module is responsible
only for *recording* what resources were observed each time a state was entered;
the diffing of those observations into discrepancies lives in
:mod:`resource_tracking.discrepancy`.  Keeping recording and calculation apart
means the recorder stays a dumb, dependency-light store while the comparison
logic can evolve independently.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from juju.resource_registry import JujuModelHandle
from kubernetes.client import ApiException  # type: ignore[import-untyped]
from kubernetes_client import KubernetesClient
from resource_registry import ResourceRegistry
from test_suite.scheduler.states import State

from .snapshot import ResourceSnapshot
from .sources import PvcSource, ResourceSource


@dataclass(frozen=True)
class ResourceObservation:
    """A set of resources observed in one model on entry into a state."""

    state: State
    model: str
    snapshots: frozenset[ResourceSnapshot]


class StateResourceTracker:
    """Records the resources observed each time a scheduler state is entered.

    The tracker performs no comparison itself; it accumulates observations that
    :func:`~resource_tracking.discrepancy.calculate_discrepancies` later diffs.
    """

    def __init__(self, sources: Sequence[ResourceSource] | None = None) -> None:
        self._sources: tuple[ResourceSource, ...] = tuple(sources) if sources is not None else (PvcSource(),)
        self._observations: list[ResourceObservation] = []

    def record(self, state: State, model: str, snapshots: frozenset[ResourceSnapshot]) -> None:
        """Record a set of observed resources for a (state, model) pair."""
        self._observations.append(ResourceObservation(state=state, model=model, snapshots=snapshots))

    def collect(
        self,
        state: State,
        kubernetes_client: KubernetesClient,
        resource_registry: ResourceRegistry,
        logger: logging.Logger,
    ) -> None:
        """Snapshot every registered model's resources and record them.

        Collection is best-effort: a model whose namespace does not exist (e.g.
        a non-Kubernetes model) is skipped rather than raising.
        """
        for handle in resource_registry.registered_handles():
            if not isinstance(handle, JujuModelHandle):
                continue
            snapshots: set[ResourceSnapshot] = set()
            for source in self._sources:
                try:
                    snapshots.update(source.collect(kubernetes_client, handle.model))
                except ApiException as exc:
                    logger.debug(f"Skipping resource snapshot for model '{handle.model}': {exc}")
            self.record(state, handle.model, frozenset(snapshots))

    def observations(self) -> tuple[ResourceObservation, ...]:
        """Return all recorded observations, in the order they were recorded."""
        return tuple(self._observations)
