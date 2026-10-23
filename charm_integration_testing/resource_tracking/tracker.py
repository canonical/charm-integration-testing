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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from test_suite.scheduler.states import State

from .collectors import ResourceCollector
from .snapshot import ResourceSnapshot


@dataclass(frozen=True)
class ResourceObservation:
    """A set of resources observed in one model on entry into a state.

    ``controller`` disambiguates ``model`` because model names are only unique
    within a controller; a CMR run can observe the same model name on two
    different controllers.
    """

    state: State
    controller: str
    model: str
    snapshots: frozenset[ResourceSnapshot]
    skips_by_application: tuple[tuple[str, frozenset[str]], ...] = ()


class StateResourceTracker:
    """Records the resources observed each time a scheduler state is entered.

    The tracker performs no comparison itself; it accumulates observations that
    :func:`~resource_tracking.discrepancy.calculate_discrepancies` later diffs.
    """

    def __init__(self) -> None:
        self._observations: list[ResourceObservation] = []

    def record(
        self,
        state: State,
        controller: str,
        model: str,
        snapshots: frozenset[ResourceSnapshot],
        skips_by_application: Mapping[str, frozenset[str]] | None = None,
    ) -> None:
        """Record resources and their resolved application skips for one scope."""
        skips = tuple(
            sorted(
                (application, frozenset(resource_types))
                for application, resource_types in (skips_by_application or {}).items()
                if resource_types
            )
        )
        self._observations.append(
            ResourceObservation(
                state=state,
                controller=controller,
                model=model,
                snapshots=snapshots,
                skips_by_application=skips,
            )
        )

    def collect(
        self,
        state: State,
        collectors: Sequence[ResourceCollector],
        logger: logging.Logger,
        skips: Mapping[tuple[str, str, str], frozenset[str]] | None = None,
    ) -> None:
        """Snapshot resources from every collector and record them for ``state``.

        Collection is best-effort: each collector skips scopes it cannot query
        rather than raising, so a partial substrate never fails the suite.
        """
        for collector in collectors:
            for collected in collector.collect(logger):
                skips_by_application = {
                    application: resource_types
                    for (controller, model, application), resource_types in (skips or {}).items()
                    if controller == collected.controller and model == collected.model
                }
                self.record(
                    state,
                    collected.controller,
                    collected.model,
                    collected.snapshots,
                    skips_by_application,
                )

    def observations(self) -> tuple[ResourceObservation, ...]:
        """Return all recorded observations, in the order they were recorded."""
        return tuple(self._observations)
