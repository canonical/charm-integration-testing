# Copyright 2025-2026 Canonical Ltd.
# See LICENSE file for licensing details.

from pathlib import Path

import pytest
from juju import JujuClient

from .scheduler.states import State


@pytest.mark.core
@pytest.mark.state(requires=State.NO_CONTROLLER, provides=State.NO_MODEL, bridge_only=True)
def test_bootstrap_controller(
    juju_client: JujuClient,
    is_cmr_test: bool,
    target_cloud: str,
    target_controller: str,
    target_controller_bootstrap_constraints: dict[str, str],
    target_controller_bootstrap_config: dict[str, str],
    target_controller_bootstrap_metadata_source: Path | None,
    neighbor_cloud: str | None,
    neighbor_controller: str | None,
    neighbor_controller_bootstrap_constraints: dict[str, str] | None,
    neighbor_controller_bootstrap_config: dict[str, str] | None,
    neighbor_controller_bootstrap_metadata_source: Path | None,
) -> None:
    # Bootstrap neighbor controller if needed
    if is_cmr_test:
        assert neighbor_cloud is not None
        assert neighbor_controller is not None
        assert neighbor_controller_bootstrap_constraints is not None
        assert neighbor_controller_bootstrap_config is not None
        juju_client.bootstrap_controller(
            cloud=neighbor_cloud,
            controller=neighbor_controller,
            controller_constraints=neighbor_controller_bootstrap_constraints,
            bootstrap_configuration=neighbor_controller_bootstrap_config,
            metadata_source=neighbor_controller_bootstrap_metadata_source,
        )

    # Bootstrap target controller
    juju_client.bootstrap_controller(
        cloud=target_cloud,
        controller=target_controller,
        controller_constraints=target_controller_bootstrap_constraints,
        bootstrap_configuration=target_controller_bootstrap_config,
        metadata_source=target_controller_bootstrap_metadata_source,
    )
