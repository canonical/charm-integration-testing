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
from .snapshot import InconsistencyCheck, PvcSnapshot, ResourceSnapshot
from .sources import KubernetesResourceSource, PvcSource
from .tracker import ResourceObservation, StateResourceTracker

__all__ = [
    "CollectedResources",
    "Discrepancy",
    "DiscrepancyEntry",
    "InconsistencyCheck",
    "KubernetesResourceCollector",
    "KubernetesResourceSource",
    "ModelResourceDiscrepancy",
    "PvcSnapshot",
    "PvcSource",
    "QualifiedSnapshot",
    "ResourceCollector",
    "ResourceDiscrepancyError",
    "ResourceObservation",
    "ResourceSnapshot",
    "StateResourceTracker",
    "calculate_discrepancies",
    "diff_snapshots",
]
