# Copyright 2024-2026 Canonical Ltd.
# See LICENSE file for licensing details.


from datetime import timedelta

import pytest
from juju import JujuClient

from .scheduler.states import State


@pytest.mark.state(requires=State.DEPLOYED)
def test_remove_and_restore_integration(
    juju_client: JujuClient,
    model: str,
    target_controller: str,
    target_application: str,
    target_endpoint: str,
    neighbor_application: str,
    neighbor_endpoint: str,
    neighbor_model: str | None,
    neighbor_controller: str | None,
) -> None:
    target_model_uri = f"{target_controller}:{model}"
    neighbor_model_uri = (
        f"{neighbor_controller}:{neighbor_model}"
        if neighbor_controller is not None and neighbor_model is not None
        else None
    )

    # Break relation
    juju_client.remove_integration(
        model=target_model_uri,
        application_1=target_application,
        endpoint_1=target_endpoint,
        application_2=neighbor_application,
        endpoint_2=neighbor_endpoint,
        neighbor_model=neighbor_model_uri,
    )

    # Wait until integration is gone
    juju_client.wait_for_removal_of_integration(
        model=target_model_uri,
        application_1=target_application,
        endpoint_1=target_endpoint,
        application_2=neighbor_application,
        endpoint_2=neighbor_endpoint,
        timeout=timedelta(minutes=10),
        neighbor_model=neighbor_model_uri,
    )

    # Readd integration
    juju_client.integrate(
        model=target_model_uri,
        application_1=target_application,
        endpoint_1=target_endpoint,
        application_2=neighbor_application,
        endpoint_2=neighbor_endpoint,
        neighbor_model=neighbor_model_uri,
    )

    # Wait to become idle
    juju_client.idle_for_period(model=target_model_uri, timeout=timedelta(minutes=15))

    # Validate all applications and relations
    juju_client.validate_model(model=target_model_uri, level="simple")
