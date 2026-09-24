# Copyright 2025-2026 Canonical Ltd.
# See LICENSE file for licensing details.

from pathlib import Path

import pytest
from juju import JujuClient

from test_suite.fixtures.controller_spec import needs_same_controller_cloud_registration

from .scheduler.states import State


@pytest.mark.state(requires=State.NO_CONTROLLER, provides=State.NO_MODEL, bridge_only=True)
def test_bootstrap_controller(
    juju_client: JujuClient,
    is_cmr_test: bool,
    same_controller: bool,
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
    # Bootstrap neighbor controller if needed. In same-controller mode the neighbor model
    # shares the target controller, so there is nothing extra to bootstrap.
    if is_cmr_test and neighbor_controller != target_controller:
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

    # Register the neighbor cloud on the target controller when the neighbor model
    # lives there (same-controller mode) but on a different cloud (SQT-884:
    # different platforms, same controller). The neighbor model is then created on
    # that cloud in test_create_model. When --current-state reuses a pre-existing
    # controller this test does not run at all; register_preexisting_neighbor_cloud
    # in conftest.py handles that case.
    if needs_same_controller_cloud_registration(
        is_cmr_test=is_cmr_test,
        same_controller=same_controller,
        neighbor_cloud=neighbor_cloud,
        target_cloud=target_cloud,
    ):
        assert neighbor_cloud is not None
        juju_client.add_cloud(cloud=neighbor_cloud, controller=target_controller)
