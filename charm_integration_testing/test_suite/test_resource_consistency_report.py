# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""End-of-suite resource consistency check.

This test carries no ``@pytest.mark.state`` marker, so the scheduler appends it
after every state-marked test and never skips it when a state transition fails.
It therefore runs once, irrespective of the pass/fail outcome of the rest of the
suite.

It reads the observations gathered by the ``track_state_resources`` fixture,
computes discrepancies (resources that went missing or appeared unexpectedly on
re-entry into a state), and emits them through the same ``execution_metadata``
mechanism used for charm metadata so they land in the JUnit ``<properties>`` and
are parsable the same way.  Metadata keys are deliberately free of per-run
identifiers (such as the model name) so they stay consistent across runs and can
drive downstream attachment rules; run-specific context lives in the value.

Unlike charm metadata, a resource inconsistency is treated as a failure: the
test asserts that no discrepancies were found, so the suite surfaces drift
rather than merely recording it.
"""

from typing import Callable

from resource_tracking import StateResourceTracker, calculate_discrepancies


def test_resource_consistency_report(
    state_resource_tracker: StateResourceTracker,
    execution_metadata: Callable[[str, str], None],
) -> None:
    discrepancies = calculate_discrepancies(state_resource_tracker.observations())

    for discrepancy in discrepancies:
        for key, value in discrepancy.report_entries():
            execution_metadata(key, value)

    assert not discrepancies, "Resource inconsistencies detected across scheduler states:\n" + "\n".join(
        discrepancy.summary() for discrepancy in discrepancies
    )
