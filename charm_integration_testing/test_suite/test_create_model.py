# Copyright 2025-2026 Canonical Ltd.
# See LICENSE file for licensing details.

import pytest
from juju import JujuClient

from .scheduler.states import State


@pytest.mark.state(requires=State.NO_MODEL, provides=State.NO_BUNDLE, bridge_only=True)
def test_create_model(
    juju_client: JujuClient, juju_controller: str, model: str, juju_model_config: dict[str, str]
) -> None:
    return juju_client.add_model(controller=juju_controller, model=model, model_config=juju_model_config)
