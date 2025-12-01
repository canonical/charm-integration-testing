# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.


from datetime import timedelta

from juju import JujuClient


def test_scale_down_scale_up_charm(juju_client: JujuClient, model: str, target_application: str) -> None:
    # Get units
    num_units = juju_client.num_units(target_application, model=model)

    # Remove units
    juju_client.scale_application(target_application, 0, model=model)

    # Wait for all units to be removed
    juju_client.wait_for_removal_of_units(target_application, model=model, timeout=timedelta(minutes=10))

    # Rescale application
    juju_client.scale_application(target_application, num_units, model=model)

    # Wait for return to idle
    juju_client.idle_for_period(model=model, timeout=timedelta(minutes=15))
