# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Calculation of resource discrepancies from recorded observations.

The same scheduler state is expected to correspond to the same set of resources
every time it is entered.  :func:`calculate_discrepancies` takes the observations
gathered by :class:`~resource_tracking.tracker.StateResourceTracker`, treats the
first observation of each state as the baseline, and reports resources that have
gone ``missing`` or appeared ``extra`` on any later visit, as well as resources
that re-appear with the *same* logical identity but a *changed* attribute -- a
resource-specific inconsistency (e.g. a PVC ``resized`` or a StatefulSet
``image_changed``).

:class:`Discrepancy` is a structural interface so that different resource
scopes can carry their own attributes; :class:`ModelResourceDiscrepancy` is the
model-scoped implementation used for Kubernetes resources.  Discrepancies expose
*structured* data (:class:`DiscrepancyEntry`) rather than pre-formatted
key-value strings: how that structure is normalised into execution metadata is a
recording concern that belongs with the recorder, not here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
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

    ``snapshot`` is the concrete resource this entry describes.  For ``extra`` and
    modification qualifiers it is the re-entry (drifted) snapshot; for ``missing``
    it is the first-visit snapshot, since the resource is absent on re-entry.
    ``baseline`` is the first-visit snapshot of the *same* logical resource and is
    only populated for modification qualifiers (e.g. ``resized``); for
    ``missing``/``extra`` there is no counterpart, so it is ``None``.
    """

    resource_type: str
    qualifier: str
    state: str
    model: str
    snapshot: ResourceSnapshot
    baseline: ResourceSnapshot | None = None


@runtime_checkable
class Discrepancy(Protocol):
    """A resource inconsistency exposing structured, normalised domain data."""

    def entries(self) -> Iterable[DiscrepancyEntry]:
        """Yield one :class:`DiscrepancyEntry` per drifted resource."""

    def summary(self) -> str:
        """Return a one-line human-readable description for failure messages."""


@dataclass(frozen=True)
class QualifiedSnapshot:
    """A drifted resource under one qualifier, with its baseline counterpart.

    ``baseline`` is ``None`` for presence qualifiers (``missing``/``extra``) and
    the first-visit snapshot for modification qualifiers, so the recorder can
    render an ``old->new`` diff.
    """

    snapshot: ResourceSnapshot
    baseline: ResourceSnapshot | None = None


@dataclass(frozen=True)
class ModelResourceDiscrepancy:
    """A resource inconsistency detected on re-entry into a state for one model.

    ``qualified`` maps each qualifier (``missing``, ``extra``, or a
    resource-specific modification kind such as ``resized``) to the resources
    that exhibit it.  Keeping a single mapping -- rather than fixed ``missing``
    and ``extra`` fields -- lets a new qualifier flow through unchanged.
    """

    state: State
    model: str
    qualified: Mapping[str, tuple[QualifiedSnapshot, ...]]

    def entries(self) -> Iterable[DiscrepancyEntry]:
        for qualifier, qualified in self.qualified.items():
            for item in qualified:
                yield DiscrepancyEntry(
                    resource_type=item.snapshot.resource_type,
                    qualifier=qualifier,
                    state=self.state.value,
                    model=self.model,
                    snapshot=item.snapshot,
                    baseline=item.baseline,
                )

    def summary(self) -> str:
        parts = [
            f"{item.snapshot.resource_type}={item.snapshot.name} ({qualifier})"
            for qualifier, qualified in self.qualified.items()
            for item in qualified
        ]
        return f"state={self.state.value} model={self.model}: " + ", ".join(parts)


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


def _modifications(
    baseline: ResourceSnapshot,
    current: ResourceSnapshot,
) -> Iterable[tuple[str, QualifiedSnapshot]]:
    """Yield ``(qualifier, QualifiedSnapshot)`` for each changed attribute.

    Runs the resource type's declared
    :attr:`~resource_tracking.snapshot.ResourceSnapshot.inconsistency_checks`,
    comparing the named report attribute on the baseline against the current
    snapshot.  Each differing attribute becomes its own qualifier so callers can
    select on it.
    """
    baseline_attributes = baseline.report_attributes()
    current_attributes = current.report_attributes()
    for check in current.inconsistency_checks:
        baseline_value = baseline_attributes.get(check.attribute)
        current_value = current_attributes.get(check.attribute)
        if baseline_value == current_value:
            continue
        if check.ignore_empty_transition and (not baseline_value or not current_value):
            continue
        yield check.qualifier, QualifiedSnapshot(snapshot=current, baseline=baseline)


def diff_snapshots(
    baseline: frozenset[ResourceSnapshot],
    current: frozenset[ResourceSnapshot],
) -> dict[str, tuple[QualifiedSnapshot, ...]]:
    """Compare a baseline snapshot set against a later one by logical identity.

    Returns a mapping of qualifier name to the resources exhibiting it, sorted by
    identity.  ``missing`` holds baseline resources gone on re-entry and ``extra``
    holds resources newly present; both carry no baseline counterpart.  Resources
    present in both sets (same ``identity``) are then run through each snapshot's
    :attr:`~resource_tracking.snapshot.ResourceSnapshot.inconsistency_checks` to
    surface resource-specific modification qualifiers (e.g. ``resized``), each
    carrying its first-visit baseline so an ``old->new`` diff can be reported.

    Qualifiers with no matching resources are omitted, so ``bool(result)`` is a
    truthy test for "some drift".
    """
    baseline_by_key = {(s.resource_type, s.identity): s for s in baseline}
    current_by_key = {(s.resource_type, s.identity): s for s in current}

    grouped: dict[str, list[QualifiedSnapshot]] = {"missing": [], "extra": []}
    for key, snapshot in baseline_by_key.items():
        if key not in current_by_key:
            grouped["missing"].append(QualifiedSnapshot(snapshot=snapshot))
    for key, snapshot in current_by_key.items():
        if key not in baseline_by_key:
            grouped["extra"].append(QualifiedSnapshot(snapshot=snapshot))
        else:
            for qualifier, qualified in _modifications(baseline_by_key[key], snapshot):
                grouped.setdefault(qualifier, []).append(qualified)

    sorted_grouped = {
        qualifier: tuple(sorted(items, key=lambda item: (item.snapshot.resource_type, item.snapshot.identity)))
        for qualifier, items in grouped.items()
        if items
    }
    ordered: dict[str, tuple[QualifiedSnapshot, ...]] = {}
    for qualifier in ("missing", "extra"):
        if qualifier in sorted_grouped:
            ordered[qualifier] = sorted_grouped.pop(qualifier)
    for qualifier in sorted(sorted_grouped):
        ordered[qualifier] = sorted_grouped[qualifier]
    return ordered


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

        qualified = diff_snapshots(baselines[key], observation.snapshots)
        if qualified:
            discrepancies.append(
                ModelResourceDiscrepancy(
                    state=observation.state,
                    model=observation.model,
                    qualified=qualified,
                )
            )

    return discrepancies
