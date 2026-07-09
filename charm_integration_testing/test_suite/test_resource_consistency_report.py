# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""End-of-suite resource consistency check.

This test carries no ``@pytest.mark.state`` marker, so the scheduler appends it
after every state-marked test and never skips it when a state transition fails.
It therefore runs once, irrespective of the pass/fail outcome of the rest of the
suite.

It reads the observations gathered by the ``track_state_resources`` fixture and
computes discrepancies.  When any are found it raises
:class:`~resource_tracking.ResourceDiscrepancyError`, which both fails the test
and carries the structured discrepancies.  The ``record_failure_execution_metadata``
fixture in ``conftest.py`` picks that exception up and normalises the
discrepancies into execution metadata, keeping metadata formatting out of the
test and the domain objects.
"""

from resource_tracking import ResourceDiscrepancyError, StateResourceTracker, calculate_discrepancies


def test_resource_consistency_report(state_resource_tracker: StateResourceTracker) -> None:
    discrepancies = calculate_discrepancies(state_resource_tracker.observations())
    if discrepancies:
        raise ResourceDiscrepancyError(discrepancies)
