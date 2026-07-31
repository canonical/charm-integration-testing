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
model-scoped implementation used for Kubernetes resources.  Discrepancies expose
*structured* data (:class:`DiscrepancyEntry`) rather than pre-formatted
key-value strings: how that structure is normalised into execution metadata is a
recording concern that belongs with the recorder, not here.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from test_suite.scheduler.states import State

from .snapshot import ResourceSnapshot
from .tracker import ResourceObservation


@dataclass(frozen=True)
class DiscrepancyEntry:
    """One drifted resource, in normalised structured form.

    The fields are deliberately generic so downstream consumers can select on
    broadly-applicable dimensions (``resource_type``, ``qualifier``) while
    treating run-specific context (``state``, ``model``, the ``snapshot``
    detail) as informational.
    """

    resource_type: str
    qualifier: str
    state: str
    model: str
    snapshot: ResourceSnapshot


@runtime_checkable
class Discrepancy(Protocol):
    """A resource inconsistency exposing structured, normalised domain data."""

    def entries(self) -> Iterable[DiscrepancyEntry]:
        """Yield one :class:`DiscrepancyEntry` per drifted resource."""

    def summary(self) -> str:
        """Return a one-line human-readable description for failure messages."""


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

    def entries(self) -> Iterable[DiscrepancyEntry]:
        for snapshot in self.missing:
            yield self._entry(snapshot, qualifier="missing")
        for snapshot in self.extra:
            yield self._entry(snapshot, qualifier="extra")

    def summary(self) -> str:
        parts = [f"{snapshot.resource_type}={snapshot.name} (missing)" for snapshot in self.missing]
        parts += [f"{snapshot.resource_type}={snapshot.name} (extra)" for snapshot in self.extra]
        return f"state={self.state.value} model={self.model}: " + ", ".join(parts)

    def _entry(self, snapshot: ResourceSnapshot, qualifier: str) -> DiscrepancyEntry:
        return DiscrepancyEntry(
            resource_type=snapshot.resource_type,
            qualifier=qualifier,
            state=self.state.value,
            model=self.model,
            snapshot=snapshot,
        )


class ResourceDiscrepancyError(Exception):
    """Raised when resource inconsistencies are detected across scheduler states.

    Carries the structured :attr:`discrepancies` so the recording layer can
    normalise them into execution metadata, keeping metadata formatting out of
    the domain objects.
    """

    def __init__(self, discrepancies: Sequence[Discrepancy]) -> None:
        self.discrepancies: tuple[Discrepancy, ...] = tuple(discrepancies)
        message = "Resource inconsistencies detected across scheduler states:\n" + "\n".join(
            discrepancy.summary() for discrepancy in self.discrepancies
        )
        super().__init__(message)


def _sorted_by_identity(snapshots: Iterable[ResourceSnapshot]) -> tuple[ResourceSnapshot, ...]:
    return tuple(sorted(snapshots, key=lambda snapshot: (snapshot.identity, snapshot.name)))


def _grouped_by_identity(
    snapshots: Iterable[ResourceSnapshot],
) -> dict[tuple[str, ...], list[ResourceSnapshot]]:
    groups: dict[tuple[str, ...], list[ResourceSnapshot]] = {}
    for snapshot in snapshots:
        groups.setdefault(snapshot.identity, []).append(snapshot)
    for group in groups.values():
        group.sort(key=lambda snapshot: snapshot.name)
    return groups


def diff_snapshots(
    baseline: frozenset[ResourceSnapshot],
    current: frozenset[ResourceSnapshot],
) -> dict[str, tuple[ResourceSnapshot, ...]]:
    """Compare a baseline snapshot multiset against a later one by resource identity.

    Returns a mapping of qualifier name to the snapshots exhibiting it.  Today
    the qualifiers are ``missing`` (in the baseline but gone) and ``extra`` (newly
    present).  Isolating this here keeps :func:`calculate_discrepancies` a thin
    orchestrator and gives a single place to add resource-specific qualifiers
    (e.g. a resized volume or a changed phase) without disturbing the diff loop.

    Compares by *count per identity*, not exact equality, since some identities
    (e.g. PVCs with a randomised name) can match several distinct objects.
    """
    baseline_groups = _grouped_by_identity(baseline)
    current_groups = _grouped_by_identity(current)

    missing: list[ResourceSnapshot] = []
    extra: list[ResourceSnapshot] = []
    for identity in baseline_groups.keys() | current_groups.keys():
        baseline_items = baseline_groups.get(identity, [])
        current_items = current_groups.get(identity, [])
        delta = len(baseline_items) - len(current_items)
        if delta > 0:
            missing.extend(baseline_items[-delta:])
        elif delta < 0:
            extra.extend(current_items[delta:])

    return {
        "missing": _sorted_by_identity(missing),
        "extra": _sorted_by_identity(extra),
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
