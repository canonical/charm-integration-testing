# Copyright 2024-2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Verify that scaling the target application to zero and back recovers cleanly.

This is a pure test: it requires the ``deployed`` state and leaves the
environment in ``deployed`` after completion (units are removed then
restored within the test body).
"""

from datetime import timedelta

import pytest
from juju import JujuClient

from .scheduler.states import State

_WAIT_FOR_REMOVAL_TIMEOUT = timedelta(minutes=10)
_WAIT_FOR_IDLE_TIMEOUT = timedelta(minutes=15)


@pytest.mark.state(requires=State.DEPLOYED)
def test_scale_in_and_scale_out(
    juju_client: JujuClient,
    model: str,
    target_application: str,
) -> None:
    """Scale the target application to zero units then back to the original count."""
    original_unit_count = juju_client.num_units(target_application, model=model)

    juju_client.scale_application(target_application, 0, model=model)
    juju_client.wait_for_removal_of_units(target_application, model=model, timeout=_WAIT_FOR_REMOVAL_TIMEOUT)

    juju_client.scale_application(target_application, original_unit_count, model=model)
    juju_client.idle_for_period(model=model, timeout=_WAIT_FOR_IDLE_TIMEOUT)
