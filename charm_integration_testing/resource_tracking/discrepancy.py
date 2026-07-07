# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Calculation of resource discrepancies from recorded observations.

The same scheduler state is expected to correspond to the same set of resources
every time it is entered.  :func:`calculate_discrepancies` takes the observations
gathered by :class:`~resource_tracking.tracker.StateResourceTracker`, treats the
first observation of each state as the baseline, and reports resources that have
gone ``missing`` or appeared ``extra`` on any later visit.

:class:`Discrepancy` is a structural interface so that different resource
scopes can carry their own attributes; :class:`ModelResourceDiscrepancy` is the
model-scoped implementation used for Kubernetes resources.  Report keys are kept
free of per-run identifiers (such as the model name) so they remain consistent
across runs and can drive downstream attachment rules; run-specific context is
carried in the value instead.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from test_suite.scheduler.states import State

from .snapshot import ResourceSnapshot
from .tracker import ResourceObservation


@runtime_checkable
class Discrepancy(Protocol):
    """A resource inconsistency that can render itself as report entries."""

    def report_entries(self) -> Iterable[tuple[str, str]]:
        """Yield stable ``(key, value)`` metadata entries describing this discrepancy."""

    def summary(self) -> str:
        """Return a one-line human-readable description for assertion messages."""


@dataclass(frozen=True)
class ModelResourceDiscrepancy:
    """A resource inconsistency detected on re-entry into a state for one model.

    ``missing`` holds baseline resources absent on re-entry; ``extra`` holds
    resources present on re-entry that were not part of the baseline.
    """

    state: State
    model: str
    missing: tuple[ResourceSnapshot, ...]
    extra: tuple[ResourceSnapshot, ...]

    def report_entries(self) -> Iterable[tuple[str, str]]:
        for snapshot in self.missing:
            yield f"resource:{snapshot.resource_type}:missing", self._describe(snapshot)
        for snapshot in self.extra:
            yield f"resource:{snapshot.resource_type}:extra", self._describe(snapshot)

    def summary(self) -> str:
        parts = [f"{snapshot.resource_type}={snapshot.name} (missing)" for snapshot in self.missing]
        parts += [f"{snapshot.resource_type}={snapshot.name} (extra)" for snapshot in self.extra]
        return f"state={self.state.value} model={self.model}: " + ", ".join(parts)

    def _describe(self, snapshot: ResourceSnapshot) -> str:
        detail = f"state={self.state.value} model={self.model} {snapshot.resource_type}={snapshot.name}"
        attributes = " ".join(f"{key}={value}" for key, value in snapshot.report_attributes().items())
        return f"{detail} {attributes}".rstrip()


def _sorted_by_identity(snapshots: Iterable[ResourceSnapshot]) -> tuple[ResourceSnapshot, ...]:
    return tuple(sorted(snapshots, key=lambda snapshot: snapshot.identity))


def diff_snapshots(
    baseline: frozenset[ResourceSnapshot],
    current: frozenset[ResourceSnapshot],
) -> dict[str, tuple[ResourceSnapshot, ...]]:
    """Compare a baseline snapshot set against a later one by resource identity.

    Returns a mapping of qualifier name to the snapshots exhibiting it.  Today
    the qualifiers are ``missing`` (in the baseline but gone) and ``extra`` (newly
    present).  Isolating this here keeps :func:`calculate_discrepancies` a thin
    orchestrator and gives a single place to add resource-specific qualifiers
    (e.g. a resized volume or a changed phase) without disturbing the diff loop.
    """
    baseline_identities = {snapshot.identity for snapshot in baseline}
    current_identities = {snapshot.identity for snapshot in current}
    return {
        "missing": _sorted_by_identity(s for s in baseline if s.identity not in current_identities),
        "extra": _sorted_by_identity(s for s in current if s.identity not in baseline_identities),
    }


def calculate_discrepancies(
    observations: Iterable[ResourceObservation],
) -> list[ModelResourceDiscrepancy]:
    """Diff each state's later observations against its first (baseline) visit."""
    baselines: dict[tuple[State, str], frozenset[ResourceSnapshot]] = {}
    discrepancies: list[ModelResourceDiscrepancy] = []

    for observation in observations:
        key = (observation.state, observation.model)
        if key not in baselines:
            baselines[key] = observation.snapshots
            continue

        qualifiers = diff_snapshots(baselines[key], observation.snapshots)
        if any(qualifiers.values()):
            discrepancies.append(
                ModelResourceDiscrepancy(
                    state=observation.state,
                    model=observation.model,
                    missing=qualifiers["missing"],
                    extra=qualifiers["extra"],
                )
            )

    return discrepancies
