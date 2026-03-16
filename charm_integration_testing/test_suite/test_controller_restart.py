# Copyright 2025-2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta

import pytest
from juju import JujuClient

from .scheduler.states import State


@pytest.mark.state(requires=State.DEPLOYED, provides=State.EMPTY_MODEL)
def test_controller_restart(juju_client: JujuClient, model: str) -> None:
    # Reboot our controllers with a rolling reboot
    juju_client.reboot_model_controller_leader(model=model)

    # Wait until idle
    juju_client.idle_for_period(model=model, timeout=timedelta(minutes=15))

    # Validate all applications and relations
    juju_client.validate_model(model=model, level="simple")


@pytest.mark.state(requires=State.EMPTY_MODEL, provides=State.NO_BUNDLE)
def test_dummy():
    pass
