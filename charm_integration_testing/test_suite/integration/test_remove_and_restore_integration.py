# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.


from datetime import timedelta

from juju import JujuClient


def test_remove_and_restore_integration(
    juju_client: JujuClient,
    model: str,
    target_application: str,
    target_endpoint: str,
    neighbor_application: str,
    neighbor_endpoint: str,
) -> None:
    # Break relation
    juju_client.remove_integration(
        model=model,
        application_1=target_application,
        application_2=neighbor_application,
        endpoint_1=target_endpoint,
        endpoint_2=neighbor_endpoint,
    )

    # Wait until integration is gone
    juju_client.wait_for_removal_of_integration(
        model=model,
        application_1=target_application,
        application_2=neighbor_application,
        endpoint_1=target_endpoint,
        endpoint_2=neighbor_endpoint,
        timeout=timedelta(minutes=10),
    )

    # Readd integration
    juju_client.integrate(
        model=model,
        application_1=target_application,
        application_2=neighbor_application,
        endpoint_1=target_endpoint,
        endpoint_2=neighbor_endpoint,
    )

    # Wait to become idle
    juju_client.idle_for_period(model=model, timeout=timedelta(minutes=15))
