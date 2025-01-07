# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.


from datetime import timedelta

import pytest

from charm_integration_testing.juju import JujuClient


@pytest.mark.timeout(timedelta(minutes=15).seconds)
def test_scale_down_scale_up_charm(juju_client: JujuClient, model: str, requirer_application: str):
    # Get units
    num_units = juju_client.num_units(requirer_application)

    # Remove units
    juju_client.scale_application(requirer_application, 0)
    juju_client.wait_idle(model=model)

    # Rescale application
    juju_client.scale_application(requirer_application, num_units)
    juju_client.wait_idle(model=model)
