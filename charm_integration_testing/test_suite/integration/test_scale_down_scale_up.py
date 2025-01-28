# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.


from datetime import timedelta

import pytest

from charm_integration_testing.juju import JujuClient


@pytest.mark.timeout(timedelta(minutes=15).total_seconds())
def test_scale_down_scale_up_charm(juju_client: JujuClient, model: str, target_application: str):
    # Get units
    num_units = juju_client.num_units(target_application, model=model)

    # Remove units
    juju_client.scale_application(target_application, 0, model=model)
    juju_client.idle_for_period(model=model)

    # Rescale application
    juju_client.scale_application(target_application, num_units, model=model)
    juju_client.idle_for_period(model=model)
