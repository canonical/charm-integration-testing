# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.


import pytest

from charm_integration_testing.juju import JujuClient


@pytest.mark.timeout(60 * 15)
def test_restart_charm(juju_client: JujuClient, requirer_application: str, provider_application: str):
    # Get units
    num_units = juju_client.num_units(requirer_application)

    # Remove units
    juju_client.scale_application(requirer_application, 0)
    juju_client.wait_idle(requirer_application, provider_application)

    # Rescale application
    juju_client.scale_application(requirer_application, num_units)
    juju_client.wait_idle(requirer_application, provider_application)
