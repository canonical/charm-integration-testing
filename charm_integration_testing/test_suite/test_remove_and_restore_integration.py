# Copyright 2024-2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Verify that removing and re-adding an integration restores full functionality.

This is a pure test: it requires the ``deployed`` state and leaves the
environment in ``deployed`` after completion (the integration is removed
then restored within the test body).
"""

from datetime import timedelta

import pytest
from juju import JujuClient

from .scheduler.states import State

_WAIT_FOR_REMOVAL_TIMEOUT = timedelta(minutes=10)
_WAIT_FOR_IDLE_TIMEOUT = timedelta(minutes=15)


@pytest.mark.state(requires=State.DEPLOYED)
def test_remove_and_restore_integration(
    juju_client: JujuClient,
    model: str,
    target_application: str,
    target_endpoint: str,
    neighbor_application: str,
    neighbor_endpoint: str,
) -> None:
    """Break the integration then re-add it and verify the model returns to idle."""
    juju_client.remove_integration(
        model=model,
        application_1=target_application,
        application_2=neighbor_application,
        endpoint_1=target_endpoint,
        endpoint_2=neighbor_endpoint,
    )

    juju_client.wait_for_removal_of_integration(
        model=model,
        application_1=target_application,
        application_2=neighbor_application,
        endpoint_1=target_endpoint,
        endpoint_2=neighbor_endpoint,
        timeout=_WAIT_FOR_REMOVAL_TIMEOUT,
    )

    juju_client.integrate(
        model=model,
        application_1=target_application,
        application_2=neighbor_application,
        endpoint_1=target_endpoint,
        endpoint_2=neighbor_endpoint,
    )

    juju_client.idle_for_period(model=model, timeout=_WAIT_FOR_IDLE_TIMEOUT)
