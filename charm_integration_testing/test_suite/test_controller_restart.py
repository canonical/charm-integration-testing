# Copyright 2025-2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta

import pytest
from juju import JujuClient, JujuModelHandle

from .scheduler.states import State


@pytest.mark.state(requires=State.DEPLOYED, provides=State.DEPLOYED)
def test_controller_restart(juju_client: JujuClient, target_model_ref: JujuModelHandle) -> None:
    # Reboot our controllers with a rolling reboot
    juju_client.reboot_model_controller(model=target_model_ref)

    # Wait until idle
    juju_client.idle_for_period(model=target_model_ref, timeout=timedelta(minutes=15))

    # Validate all applications and relations
    juju_client.validate_model(model=target_model_ref, level="deep")
