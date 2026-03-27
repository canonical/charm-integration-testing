# Copyright 2025-2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta

import pytest
from juju import JujuClient

from .scheduler.states import State


@pytest.mark.state(requires=State.DEPLOYED, provides=State.UPGRADED_FROM_OLD_REVISION)
def test_downgrade_charm(
    juju_client: JujuClient,
    historical_revision_with_passing_deploy: int | None,
    model: str,
    target_charm: str,
    target_application: str,
    target_revision: int | None,
    target_channel: str | None,
) -> None:
    if target_revision is None:
        pytest.fail("--target-revision must be provided as an integer for this test.")

    # Use historical revision selected by the fixture.
    selected_revision = historical_revision_with_passing_deploy

    if selected_revision is None:
        pytest.fail(
            "Unable to find a historical revision with a passing test_deploy result "
            f"for charm '{target_charm}' in channel '{target_channel}'."
        )

    juju_client.logger.info(
        f"Selected historical revision {selected_revision} for {target_application} ({target_charm})"
    )

    # Downgrading the charm to the target revision specified by the fixture
    juju_client.logger.info(
        f"Refreshing {target_application} from current revision "
        f"{target_revision} to older bundle revision {selected_revision}."
    )
    juju_client.refresh_application(
        application=target_application,
        revision=selected_revision,
        channel=target_channel,
        model=model,
    )
    juju_client.wait_for_application_revision(
        application=target_application,
        expected_revision=selected_revision,
        model=model,
        timeout=timedelta(minutes=5),
    )
    juju_client.idle_for_period(model=model, timeout=timedelta(minutes=15))

    # Verify the application is downgraded to the selected revision and the model is healthy
    downgraded_revision = juju_client.application_revision(application=target_application, model=model)
    if downgraded_revision != selected_revision:
        pytest.fail(
            f"Expected '{target_application}' to be on downgraded revision "
            f"{selected_revision}, got {downgraded_revision}."
        )
    juju_client.validate_model(model=model, level="simple")
