# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.


from datetime import timedelta

import pytest

from charm_integration_testing.juju import JujuClient


@pytest.mark.timeout(timedelta(minutes=15).total_seconds())
def test_break_readd_integration(
    juju_client: JujuClient,
    model: str,
    requirer_application: str,
    requirer_endpoint: str,
    provider_application: str,
    provider_endpoint: str,
):
    # Remove units
    juju_client.remove_integration(
        model=model,
        application_1=requirer_application,
        application_2=provider_application,
        endpoint_1=requirer_endpoint,
        endpoint_2=provider_endpoint,
    )
    juju_client.idle_for_period(model=model)

    # Rescale application
    juju_client.integrate(
        model=model,
        application_1=requirer_application,
        application_2=provider_application,
        endpoint_1=requirer_endpoint,
        endpoint_2=provider_endpoint,
    )
    juju_client.idle_for_period(model=model)
