# Copyright 2025-2026 Canonical Ltd.
# See LICENSE file for licensing details.


from datetime import timedelta

import pytest
from juju import JujuClient, JujuModelHandle

from .scheduler.states import State


@pytest.mark.state(requires=State.DEPLOYED, provides=State.NEIGHBOR_ONLY)
def test_teardown(juju_client: JujuClient, target_model_ref: JujuModelHandle, target_application: str) -> None:
    # Remove all requested applications
    juju_client.remove_applications(target_application, model=target_model_ref)

    # Wait until application has been removed
    juju_client.wait_for_removal(target_application, model=target_model_ref, timeout=timedelta(minutes=15))
