# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""State-associated resource tracking for the test suite.

The scheduler drives the environment through a series of canonical
:class:`~test_suite.scheduler.states.State` values.  The same state is expected
to correspond to the same set of resources every time it is entered.  This
module records a baseline the first time a state is seen and, on every
subsequent visit, reports any resources that have gone ``missing`` or appeared
``extra`` relative to that baseline.

The tracker works against the :class:`ResourceSnapshot` structural interface, so
it is agnostic to the concrete resource type; new resource kinds are added by
providing a snapshot type that satisfies that interface.  It is intentionally
free of pytest and Kubernetes dependencies so it can be unit-tested in
isolation; callers feed it snapshots and read back discrepancies.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..scheduler.states import State
from .snapshot import ResourceSnapshot


@dataclass(frozen=True)
class Discrepancy:
    """A resource inconsistency detected on re-entry into a state.

    ``missing`` holds baseline resources absent on re-entry; ``extra`` holds
    resources present on re-entry that were not part of the baseline.
    """

    state: State
    model: str
    missing: tuple[ResourceSnapshot, ...]
    extra: tuple[ResourceSnapshot, ...]


class StateResourceTracker:
    """Records resource baselines per (state, model) and diffs subsequent visits."""

    def __init__(self) -> None:
        # (state, model) -> baseline snapshot set captured on first visit.
        self._baselines: dict[tuple[State, str], frozenset[ResourceSnapshot]] = {}
        self._discrepancies: list[Discrepancy] = []

    def record(self, state: State, model: str, snapshots: frozenset[ResourceSnapshot]) -> None:
        """Record a snapshot for a (state, model) pair.

        The first snapshot for a given pair becomes the baseline.  Every later
        snapshot for the same pair is diffed against that baseline: baseline
        resources absent from the new snapshot are recorded as ``missing`` and
        resources newly present are recorded as ``extra``.
        """
        key = (state, model)
        if key not in self._baselines:
            self._baselines[key] = snapshots
            return

        baseline = self._baselines[key]
        baseline_identities = {snapshot.identity for snapshot in baseline}
        current_identities = {snapshot.identity for snapshot in snapshots}
        missing = tuple(
            sorted(
                (snapshot for snapshot in baseline if snapshot.identity not in current_identities),
                key=lambda snapshot: snapshot.identity,
            )
        )
        extra = tuple(
            sorted(
                (snapshot for snapshot in snapshots if snapshot.identity not in baseline_identities),
                key=lambda snapshot: snapshot.identity,
            )
        )
        if missing or extra:
            self._discrepancies.append(
                Discrepancy(state=state, model=model, missing=missing, extra=extra)
            )

    def baselines(self) -> dict[tuple[State, str], frozenset[ResourceSnapshot]]:
        """Return a copy of the recorded baselines."""
        return dict(self._baselines)

    def discrepancies(self) -> list[Discrepancy]:
        """Return all discrepancies detected so far, in detection order."""
        return list(self._discrepancies)
