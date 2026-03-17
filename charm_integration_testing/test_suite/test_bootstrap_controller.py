# Copyright 2025-2026 Canonical Ltd.
# See LICENSE file for licensing details.

import pytest
from juju import JujuClient

from .scheduler.states import State


@pytest.mark.state(requires=State.NO_CONTROLLER, provides=State.NO_MODEL, bridge_only=True)
def test_bootstrap_controller(
    juju_client: JujuClient,
    juju_cloud: str,
    juju_controller: str,
) -> None:
    return juju_client.bootstrap_controller(cloud=juju_cloud, controller=juju_controller)
