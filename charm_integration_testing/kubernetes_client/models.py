# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True)
class PvcSnapshot:
    """Immutable point-in-time view of a single PersistentVolumeClaim.

    Frozen and hashable so snapshots can be collected into sets and diffed
    across repeated visits to the same scheduler state.

    Satisfies the ``ResourceSnapshot`` structural interface consumed by the test
    suite's resource tracker, so it can be tracked and reported without the
    tracker depending on this concrete type.
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
