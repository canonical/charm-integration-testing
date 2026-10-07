# Copyright 2025-2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta

import pytest
from juju import JujuClient, JujuModelHandle

from bundle_builder_x import Charm

from .scheduler.states import State


@pytest.mark.state(requires=State.DEPLOYED_WITH_OLD_REVISION, provides=State.DEPLOYED)
def test_upgrade_charm(
    juju_client: JujuClient,
    target_downgrade_revision: int,
    target_model_ref: JujuModelHandle,
    neighbor_model_ref: JujuModelHandle | None,
    target_application: str,
    target_channel: str | None,
    target_resolved_charm: Charm,
) -> None:
    # The resolved charm carries the concrete revision and channel even in a "latest release" run,
    # where --target-revision/--target-channel are left at their defaults.
    assert target_resolved_charm.revision is not None
    assert target_resolved_charm.channel is not None
    target_revision = target_resolved_charm.revision
    resolved_target_channel = target_channel or str(target_resolved_charm.channel)

    # Upgrading the charm to the target revision specified by the fixture
    juju_client.logger.info(
        f"Refreshing {target_application} from downgrade revision {target_downgrade_revision} "
        f"to original bundle revision {target_revision}."
    )
    juju_client.refresh_application(
        application=target_application,
        revision=target_revision,
        channel=resolved_target_channel,
        model=target_model_ref,
    )
    juju_client.wait_for_application_revision(
        application=target_application,
        expected_revision=target_revision,
        model=target_model_ref,
        timeout=timedelta(minutes=5),
    )
    models_to_validate = [m for m in (target_model_ref, neighbor_model_ref) if m is not None]

    # Wait for return to idle
    juju_client.multi_model_idle_for_period(models_to_validate, timeout=timedelta(minutes=15))

    # Verify the application is upgraded to the target revision and the model is healthy
    upgraded_revision = juju_client.application_revision(application=target_application, model=target_model_ref)
    if upgraded_revision != target_revision:
        pytest.fail(
            f"Expected '{target_application}' to be on upgraded revision "
            f"{target_revision}, got {upgraded_revision}."
        )
    # For a CMR the persistence validator lives on the neighbor's requirer units, so validate there too.
    for model_ref in models_to_validate:
        juju_client.validate_model(model=model_ref, level="simple")
