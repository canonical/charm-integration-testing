# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""State-associated resource tracking for the test suite."""

from .collectors import CollectedResources, KubernetesResourceCollector, ResourceCollector
from .discrepancy import (
    Discrepancy,
    DiscrepancyEntry,
    ModelResourceDiscrepancy,
    QualifiedSnapshot,
    ResourceDiscrepancyError,
    calculate_discrepancies,
    diff_snapshots,
)
from .snapshot import InconsistencyCheck, ResourceSnapshot
from .sources import DEFAULT_KUBERNETES_SOURCES, KubernetesResourceSource
from .tracker import ResourceObservation, StateResourceTracker

__all__ = [
    "DEFAULT_KUBERNETES_SOURCES",
    "CollectedResources",
    "Discrepancy",
    "DiscrepancyEntry",
    "InconsistencyCheck",
    "KubernetesResourceCollector",
    "KubernetesResourceSource",
    "ModelResourceDiscrepancy",
    "QualifiedSnapshot",
    "ResourceCollector",
    "ResourceDiscrepancyError",
    "ResourceObservation",
    "ResourceSnapshot",
    "StateResourceTracker",
    "calculate_discrepancies",
    "diff_snapshots",
]
