# Copyright 2025-2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta

import pytest
from juju import JujuClient, JujuModelHandle

from bundle_builder_x import Charm

from .scheduler.states import State


@pytest.mark.state(requires=State.DEPLOYED, provides=State.DEPLOYED_WITH_OLD_REVISION)
def test_downgrade_charm(
    juju_client: JujuClient,
    target_downgrade_revision: int | None,
    target_model_ref: JujuModelHandle,
    neighbor_model_ref: JujuModelHandle | None,
    target_application: str,
    target_resolved_charm_for_refresh: Charm | None,
) -> None:
    if target_downgrade_revision is None or target_resolved_charm_for_refresh is None:
        pytest.skip("A resolvable target and compatible downgrade revision are required for the refresh cycle.")

    juju_client.logger.info(
        f"Selected historical revision {target_downgrade_revision} for "
        f"{target_application} ({target_resolved_charm_for_refresh.name})"
    )

    # Downgrading the charm to the target revision specified by the fixture
    juju_client.logger.info(
        f"Refreshing {target_application} from current revision "
        f"{target_resolved_charm_for_refresh.revision} to older bundle revision {target_downgrade_revision}."
    )
    juju_client.refresh_application(
        application=target_application,
        revision=target_downgrade_revision,
        channel=str(target_resolved_charm_for_refresh.channel),
        model=target_model_ref,
    )
    juju_client.wait_for_application_revision(
        application=target_application,
        expected_revision=target_downgrade_revision,
        model=target_model_ref,
        timeout=timedelta(minutes=5),
    )
    models_to_validate = [m for m in (target_model_ref, neighbor_model_ref) if m is not None]

    # Wait for return to idle
    juju_client.multi_model_idle_for_period(models_to_validate, timeout=timedelta(minutes=15))

    # Verify the application is downgraded to the selected revision and the model is healthy
    downgraded_revision = juju_client.application_revision(application=target_application, model=target_model_ref)
    if downgraded_revision != target_downgrade_revision:
        pytest.fail(
            f"Expected '{target_application}' to be on downgraded revision "
            f"{target_downgrade_revision}, got {downgraded_revision}."
        )
    # For a CMR the persistence validator lives on the neighbor's requirer units, so validate there too.
    for model_ref in models_to_validate:
        juju_client.validate_model(model=model_ref, level="simple")
