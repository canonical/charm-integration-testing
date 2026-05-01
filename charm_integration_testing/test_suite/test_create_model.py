# Copyright 2025-2026 Canonical Ltd.
# See LICENSE file for licensing details.

import pytest
from juju import JujuClient

from .scheduler.states import State


@pytest.mark.core
@pytest.mark.state(requires=State.NO_MODEL, provides=State.EMPTY_MODEL, bridge_only=True)
def test_create_model(
    juju_client: JujuClient, juju_controller: str, model: str, juju_model_config: dict[str, str]
) -> None:
    juju_client.add_model(controller=juju_controller, model=model, model_config=juju_model_config)
