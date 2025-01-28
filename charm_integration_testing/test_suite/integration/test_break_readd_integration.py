# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.


from datetime import timedelta

import pytest

from charm_integration_testing.juju import JujuClient


@pytest.mark.timeout(timedelta(minutes=15).total_seconds())
def test_break_readd_integration(
    juju_client: JujuClient,
    model: str,
    target_application: str,
    target_endpoint: str,
    neighbor_application: str,
    neighbor_endpoint: str,
):
    # Break relation
    juju_client.remove_integration(
        model=model,
        application_1=target_application,
        application_2=neighbor_application,
        endpoint_1=target_endpoint,
        endpoint_2=neighbor_endpoint,
    )
    juju_client.idle_for_period(model=model)

    # Readd relation
    juju_client.integrate(
        model=model,
        application_1=target_application,
        application_2=neighbor_application,
        endpoint_1=target_endpoint,
        endpoint_2=neighbor_endpoint,
    )
    juju_client.idle_for_period(model=model)
