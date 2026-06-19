# Copyright 2025-2026 Canonical Ltd.
# See LICENSE file for licensing details.

import pytest
from juju import JujuClient

from .scheduler.states import State


@pytest.mark.state(requires=State.NO_MODEL, provides=State.EMPTY_MODEL, bridge_only=True)
def test_create_model(
    juju_client: JujuClient,
    is_cmr_test: bool,
    target_controller: str,
    model: str,
    target_model_config: dict[str, str],
    neighbor_controller: str | None,
    neighbor_model: str | None,
    neighbor_model_config: dict[str, str] | None,
) -> None:
    # Create neighbor model
    if is_cmr_test:
        assert neighbor_controller is not None
        assert neighbor_model is not None
        assert neighbor_model_config is not None
        juju_client.add_model(controller=neighbor_controller, model=neighbor_model, model_config=neighbor_model_config)

    # Create target model
    juju_client.add_model(controller=target_controller, model=model, model_config=target_model_config)
