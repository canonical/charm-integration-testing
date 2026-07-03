# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""End-of-suite resource consistency report.

This test carries no ``@pytest.mark.state`` marker, so the scheduler appends it
after every state-marked test and never skips it when a state transition fails.
It therefore runs once, irrespective of the pass/fail outcome of the rest of the
suite, and serialises the resource baselines and discrepancies gathered by the
``track_state_resources`` fixture.

The report is emitted through the same ``execution_metadata`` mechanism used for
charm metadata, so it lands in the JUnit ``<properties>`` and is parsable the
same way.  It never asserts: resource inconsistencies are reported, not enforced.

Category names are derived from each snapshot's ``resource_type``, so the report
covers any resource kind that satisfies the ``ResourceSnapshot`` interface
without changes here:

- ``resource:<type>:baseline:<state>:<model>`` -- a baseline resource name.
- ``resource:<type>:missing:<state>:<model>`` -- a baseline resource absent on
  re-entry.
- ``resource:<type>:extra:<state>:<model>`` -- a resource present on re-entry
  that was not part of the baseline.
- ``resource:<type>:inconsistency`` -- a human-readable summary line per
  missing/extra resource.
"""

from typing import Callable

from test_suite.resource_tracking import Discrepancy, ResourceSnapshot, StateResourceTracker


def _report_discrepancy(
    discrepancy: Discrepancy,
    kind: str,
    snapshots: tuple[ResourceSnapshot, ...],
    execution_metadata: Callable[[str, str], None],
) -> None:
    for snapshot in snapshots:
        execution_metadata(
            f"resource:{snapshot.resource_type}:{kind}:{discrepancy.state.value}:{discrepancy.model}",
            snapshot.name,
        )
        detail = (
            f"state={discrepancy.state.value} model={discrepancy.model} kind={kind} "
            f"{snapshot.resource_type}={snapshot.name}"
        )
        attributes = " ".join(f"{key}={value}" for key, value in snapshot.report_attributes().items())
        if attributes:
            detail = f"{detail} {attributes}"
        execution_metadata(f"resource:{snapshot.resource_type}:inconsistency", detail)


def test_resource_consistency_report(
    state_resource_tracker: StateResourceTracker,
    execution_metadata: Callable[[str, str], None],
) -> None:
    for (state, model), snapshots in state_resource_tracker.baselines().items():
        for snapshot in snapshots:
            execution_metadata(
                f"resource:{snapshot.resource_type}:baseline:{state.value}:{model}",
                snapshot.name,
            )

    for discrepancy in state_resource_tracker.discrepancies():
        _report_discrepancy(discrepancy, "missing", discrepancy.missing, execution_metadata)
        _report_discrepancy(discrepancy, "extra", discrepancy.extra, execution_metadata)
