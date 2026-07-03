# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Structural interface for resources that can be tracked across states."""

from __future__ import annotations

from typing import Mapping, Protocol


class ResourceSnapshot(Protocol):
    """Structural contract a resource snapshot must satisfy to be tracked.

    The tracker and the end-of-suite report depend only on this interface, never
    on a concrete resource type.  A new resource kind (e.g. a Secret or a
    Service) becomes trackable simply by providing a frozen, hashable snapshot
    type that implements these members; no change to the tracker or the report
    is required.
    """

    resource_type: str
    """Short label used in report categories, e.g. ``pvc``."""

    name: str
    """Human-readable resource name used in reports."""

    @property
    def identity(self) -> tuple[str, ...]:
        """Stable identity used to diff snapshots across repeated state visits."""

    def report_attributes(self) -> Mapping[str, str]:
        """Resource-specific ``key=value`` attributes for the inconsistency line."""
