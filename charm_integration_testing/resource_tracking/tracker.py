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

The tracker is substrate-agnostic: it delegates the *how* of gathering
snapshots to :class:`~resource_tracking.collectors.ResourceCollector`
implementations, so adding an ``lxd`` or ``openstack`` collector requires no
change here.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from test_suite.scheduler.states import State

from .collectors import CollectedResources, ResourceCollector
from .snapshot import ResourceSnapshot

#: Default number of times a (state, model) pair is re-collected before a
#: resource present in the baseline but absent from the latest snapshot is
#: accepted as genuinely missing. See :meth:`StateResourceTracker._settle`.
DEFAULT_MAX_SETTLE_ATTEMPTS = 3

#: Default delay, in seconds, between settle re-collection attempts.
DEFAULT_SETTLE_DELAY_SECONDS = 2.0


@dataclass(frozen=True)
class ResourceObservation:
    """A set of resources observed in one model on entry into a state."""

    state: State
    model: str
    snapshots: frozenset[ResourceSnapshot]


class StateResourceTracker:
    """Records the resources observed each time a scheduler state is entered.

    The tracker performs no discrepancy calculation itself; it accumulates
    observations that :func:`~resource_tracking.discrepancy.calculate_discrepancies`
    later diffs. It does, however, guard against a specific source of false
    positives: substrates such as Kubernetes are only *eventually* consistent,
    so a resource that is part of a state's baseline can briefly fail to appear
    in a list call taken moments later even though it still exists (see the
    ``resource_tracking`` timing race reported against ``postgresql-k8s-pgdata``
    PVCs). Before accepting a re-collected snapshot that appears to have lost a
    baseline resource, the tracker re-queries the same collector a few times
    with a short delay to let the substrate settle, and only records the
    result as final once the resource re-appears or the retry budget is spent.
    """

    def __init__(
        self,
        *,
        max_settle_attempts: int = DEFAULT_MAX_SETTLE_ATTEMPTS,
        settle_delay_seconds: float = DEFAULT_SETTLE_DELAY_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._observations: list[ResourceObservation] = []
        self._max_settle_attempts = max_settle_attempts
        self._settle_delay_seconds = settle_delay_seconds
        self._sleep = sleep

    def record(self, state: State, model: str, snapshots: frozenset[ResourceSnapshot]) -> None:
        """Record a set of observed resources for a (state, model) pair."""
        self._observations.append(ResourceObservation(state=state, model=model, snapshots=snapshots))

    def collect(
        self,
        state: State,
        collectors: Sequence[ResourceCollector],
        logger: logging.Logger,
    ) -> None:
        """Snapshot resources from every collector and record them for ``state``.

        Collection is best-effort: each collector skips scopes it cannot query
        rather than raising, so a partial substrate never fails the suite. When
        a (state, model) pair has already been visited, the freshly collected
        snapshot is allowed to settle (see :meth:`_settle`) before it is
        recorded, so a resource that has not finished re-appearing after a
        substrate-side change is not mistaken for a genuine discrepancy.
        """
        for collector in collectors:
            for collected in collector.collect(logger):
                snapshots = self._settle(state, collector, collected, logger)
                self.record(state, collected.model, snapshots)

    def observations(self) -> tuple[ResourceObservation, ...]:
        """Return all recorded observations, in the order they were recorded."""
        return tuple(self._observations)

    def _settle(
        self,
        state: State,
        collector: ResourceCollector,
        collected: CollectedResources,
        logger: logging.Logger,
    ) -> frozenset[ResourceSnapshot]:
        """Re-collect ``collected`` until baseline resources reappear or attempts run out.

        Returns the latest snapshot: either one where the baseline resources
        have reappeared, or the final attempt's snapshot if they never do
        (a genuine discrepancy).
        """
        baseline = self._baseline(state, collected.model)
        snapshots = collected.snapshots
        if baseline is None:
            return snapshots

        attempt = 1
        while attempt < self._max_settle_attempts and self._has_newly_missing(baseline, snapshots):
            logger.debug(
                "Resource(s) present in the '%s' baseline for model '%s' are absent from the "
                "latest snapshot; retrying collection (attempt %d/%d) in case this is a transient "
                "substrate timing gap rather than a genuine discrepancy.",
                state.value,
                collected.model,
                attempt,
                self._max_settle_attempts,
            )
            self._sleep(self._settle_delay_seconds)
            snapshots = self._recollect(collector, collected.model, logger)
            attempt += 1
        return snapshots

    @staticmethod
    def _recollect(collector: ResourceCollector, model: str, logger: logging.Logger) -> frozenset[ResourceSnapshot]:
        for collected in collector.collect(logger):
            if collected.model == model:
                return collected.snapshots
        return frozenset()

    def _baseline(self, state: State, model: str) -> frozenset[ResourceSnapshot] | None:
        for observation in self._observations:
            if observation.state == state and observation.model == model:
                return observation.snapshots
        return None

    @staticmethod
    def _has_newly_missing(
        baseline: frozenset[ResourceSnapshot],
        current: frozenset[ResourceSnapshot],
    ) -> bool:
        current_identities = {snapshot.identity for snapshot in current}
        return any(snapshot.identity not in current_identities for snapshot in baseline)
