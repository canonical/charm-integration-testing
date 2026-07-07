# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Resource snapshot types tracked across scheduler states.

A snapshot is an immutable, hashable view of a single cluster resource.  The
tracker and discrepancy calculator depend only on the :class:`ResourceSnapshot`
structural interface, never on a concrete resource type, so a new resource kind
becomes trackable simply by adding a snapshot type that implements it -- no
change to the tracker, calculator, or report is required.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Mapping, Protocol, runtime_checkable


@runtime_checkable
class ResourceSnapshot(Protocol):
    """Structural contract a resource snapshot must satisfy to be tracked."""

    resource_type: str
    """Short label used in report keys, e.g. ``pvc``."""

    name: str
    """Human-readable resource name used in reports."""

    @property
    def identity(self) -> tuple[str, ...]:
        """Stable identity used to diff snapshots across repeated state visits."""

    def report_attributes(self) -> Mapping[str, str]:
        """Resource-specific ``key=value`` attributes for the report line."""


@dataclass(frozen=True)
class PvcSnapshot:
    """Immutable point-in-time view of a single PersistentVolumeClaim.

    Frozen and hashable so snapshots can be collected into sets and diffed
    across repeated visits to the same scheduler state.
    """

    name: str
    namespace: str
    storage_class: str
    requested_storage: str
    phase: str

    resource_type: ClassVar[str] = "pvc"

    @property
    def identity(self) -> tuple[str, str, str, str]:
        """Stable identity used to diff snapshots across state visits.

        Excludes the volatile ``phase`` field so that a claim which is merely
        transitioning (e.g. ``Pending`` vs ``Bound``) is not reported as a
        different resource.
        """
        return (self.namespace, self.name, self.storage_class, self.requested_storage)

    def report_attributes(self) -> dict[str, str]:
        """Resource-specific ``key=value`` attributes for the report line."""
        return {
            "storage_class": self.storage_class,
            "requested_storage": self.requested_storage,
        }
