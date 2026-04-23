# Copyright 2025-2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta

import pytest
from juju import JujuClient

from .scheduler.states import State


@pytest.mark.state(requires=State.DEPLOYED, provides=State.DEPLOYED_WITH_OLD_REVISION)
def test_downgrade_charm(
    juju_client: JujuClient,
    target_downgrade_revision: int,
    model: str,
    target_charm: str,
    target_application: str,
    target_revision: int | None,
    target_channel: str | None,
) -> None:
    juju_client.logger.info(
        f"Selected historical revision {target_downgrade_revision} for {target_application} ({target_charm})"
    )

    # Downgrading the charm to the target revision specified by the fixture
    juju_client.logger.info(
        f"Refreshing {target_application} from current revision "
        f"{target_revision} to older bundle revision {target_downgrade_revision}."
    )
    juju_client.refresh_application(
        application=target_application,
        revision=target_downgrade_revision,
        channel=target_channel,
        model=model,
    )
    juju_client.wait_for_application_revision(
        application=target_application,
        expected_revision=target_downgrade_revision,
        model=model,
        timeout=timedelta(minutes=5),
    )
    juju_client.idle_for_period(model=model, timeout=timedelta(minutes=15))

    # Verify the application is downgraded to the selected revision and the model is healthy
    downgraded_revision = juju_client.application_revision(application=target_application, model=model)
    if downgraded_revision != target_downgrade_revision:
        pytest.fail(
            f"Expected '{target_application}' to be on downgraded revision "
            f"{target_downgrade_revision}, got {downgraded_revision}."
        )
    juju_client.validate_model(model=model, level="simple")
