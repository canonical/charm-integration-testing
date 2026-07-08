# Copyright 2024-2026 Canonical Ltd.
# See LICENSE file for licensing details.


from datetime import timedelta

import pytest
from juju import JujuClient, JujuIntegrationApplication

from .scheduler.states import State


@pytest.mark.state(requires=State.DEPLOYED)
def test_remove_and_restore_integration(
    juju_client: JujuClient,
    integration_controller: str,
    integration_model: str,
    integration_endpoint_1: JujuIntegrationApplication,
    integration_endpoint_2: JujuIntegrationApplication,
) -> None:
    model_uri = f"{integration_controller}:{integration_model}"

    # Break relation
    juju_client.remove_integration(
        model=model_uri,
        endpoint_1=integration_endpoint_1,
        endpoint_2=integration_endpoint_2,
    )

    # Wait until integration is gone
    juju_client.wait_for_removal_of_integration(
        model=model_uri,
        endpoint_1=integration_endpoint_1,
        endpoint_2=integration_endpoint_2,
        timeout=timedelta(minutes=10),
    )

    # Re-add integration
    juju_client.integrate(
        model=model_uri,
        endpoint_1=integration_endpoint_1,
        endpoint_2=integration_endpoint_2,
    )

    # Wait to become idle
    juju_client.idle_for_period(model=model_uri, timeout=timedelta(minutes=15))

    # Validate all applications and relations
    juju_client.validate_model(model=model_uri, level="simple")
