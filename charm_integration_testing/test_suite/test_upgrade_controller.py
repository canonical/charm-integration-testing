# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta

import pytest
from juju import JujuClient, JujuModelHandle, JujuVersion
from utils.juju_releases import UpgradeMode, classify_upgrade_mode

from .scheduler.states import State


@pytest.mark.state(requires=State.DEPLOYED, provides=State.DEPLOYED_WITH_UPGRADED_CONTROLLER)
def test_upgrade_controller(
    juju_client: JujuClient,
    target_controller: str,
    target_upgrade_version: JujuVersion | None,
    model: str,
    request: pytest.FixtureRequest,
) -> None:
    """
    Upgrade the Juju controller and verify the deployment remains healthy.

    Patch upgrades (same major.minor) use ``juju upgrade-controller``.
    Minor/major upgrades use model migration to a new controller at the target version.

    If no upgrade target is available the test is skipped automatically.
    """
    # GIVEN the resolved upgrade target
    if target_upgrade_version is None:
        # No upgrade target is available, so skip instead of reporting the
        # upgraded-controller state as provided.
        pytest.skip("No Juju upgrade target version is available for this environment.")

    target_version_str = str(target_upgrade_version)
    controller_model = JujuModelHandle(controller=target_controller, model="controller")

    # GIVEN a healthy deployment
    pre_version = juju_client.version(controller_model)

    # Classify the upgrade mode
    upgrade_mode = classify_upgrade_mode(pre_version, target_upgrade_version)

    if upgrade_mode == UpgradeMode.PATCH:
        _upgrade_in_place(juju_client, target_controller, target_version_str, model)
        active_controller = target_controller
    else:
        # Migration path: bootstrap a new controller at the target version
        # (handled by the fixture), migrate the model to it, and upgrade the
        # model agents to match the new controller.
        temp_controller: str = request.getfixturevalue("juju_controller_at_version")
        _upgrade_via_migration(juju_client, target_controller, temp_controller, target_version_str, model)
        active_controller = temp_controller

    # THEN the controller version should have advanced
    active_controller_model = JujuModelHandle(controller=active_controller, model="controller")
    post_version = juju_client.version(active_controller_model)
    assert (
        post_version > pre_version
    ), f"Expected controller version to increase after upgrade, but got {post_version} (was {pre_version})."

    # And the workload model should still be healthy
    juju_client.validate_model(model=JujuModelHandle(controller=active_controller, model=model), level="deep")


def _upgrade_in_place(
    juju_client: JujuClient,
    controller: str,
    target_version: str,
    model: str,
) -> None:
    """Perform an in-place controller upgrade via ``juju upgrade-controller``."""
    juju_client.upgrade_controller(controller=controller, agent_version=target_version)

    # Wait for the controller model to settle after the upgrade
    juju_client.idle_for_period(
        model=JujuModelHandle(controller=controller, model="controller"), timeout=timedelta(minutes=15)
    )

    # Wait for the workload model to re-stabilise
    juju_client.idle_for_period(
        model=JujuModelHandle(controller=controller, model=model), timeout=timedelta(minutes=15)
    )


def _upgrade_via_migration(
    juju_client: JujuClient,
    source_controller: str,
    target_controller: str,
    target_version: str,
    model: str,
) -> None:
    """Upgrade by migrating the model to a new controller and upgrading model agents.

    Follows the Juju documentation for minor/major controller upgrades:
    1. A new controller at the target version has already been bootstrapped
       (handled by the ``juju_controller_at_version`` fixture).
    2. Migrate the model from the old controller to the new one.
    3. Upgrade the model agents to match the new controller's version.
    """
    # Migrate model to the new-version controller
    juju_client.migrate_model(
        model_name=model, source_controller=source_controller, target_controller=target_controller
    )
    new_model_ref = JujuModelHandle(controller=target_controller, model=model)
    juju_client.wait_for_model_to_exist(model=new_model_ref, timeout=timedelta(minutes=15))
    juju_client.idle_for_period(model=new_model_ref, timeout=timedelta(minutes=15))

    # Upgrade model agents to match the new controller's version
    juju_client.upgrade_model(model=new_model_ref, agent_version=target_version)
    juju_client.idle_for_period(model=new_model_ref, timeout=timedelta(minutes=15))
