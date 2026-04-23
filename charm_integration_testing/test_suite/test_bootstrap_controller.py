# Copyright 2025-2026 Canonical Ltd.
# See LICENSE file for licensing details.

import pytest
from juju import JujuClient

from .scheduler.states import State


@pytest.mark.state(requires=State.NO_CONTROLLER, provides=State.NO_MODEL, bridge_only=True)
def test_bootstrap_controller(
    juju_client: JujuClient,
    # FIXME(@motjuste): we should just use all_bundles
    juju_cloud: str,
    juju_controller: str,
    juju_controller_bootstrap_constraints: dict[str, str],
    all_bundles: list[tuple[str, str]],
) -> None:
    if not len(all_bundles):
        all_bundles = [("unused", f"{juju_cloud}:{juju_controller}:irrelevant")]

    for _, model_info in all_bundles:
        juju_cloud, juju_controller, *_ = model_info.split(":")
        juju_client.bootstrap_controller(
            cloud=juju_cloud, controller=juju_controller, controller_constraints=juju_controller_bootstrap_constraints
        )
