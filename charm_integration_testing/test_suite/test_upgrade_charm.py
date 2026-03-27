# Copyright 2025-2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta

import pytest
from juju import JujuClient

from .scheduler.states import State


@pytest.mark.state(requires=State.DEPLOYED_WITH_OLD_REVISION, provides=State.UPGRADED_FROM_OLD_REVISION)
def test_upgrade_charm(
    juju_client: JujuClient,
    model: str,
    target_application: str,
    target_revision: int | None,
    target_channel: str | None,
) -> None:
    if target_revision is None:
        pytest.fail("--target-revision must be provided as an integer for this test.")

    # Upgrading the charm to the target revision specified by the fixture
    juju_client.logger.info(f"Refreshing {target_application} to original bundle revision {target_revision}.")
    juju_client.refresh_application(
        application=target_application,
        revision=target_revision,
        channel=target_channel,
        model=model,
    )
    juju_client.wait_for_application_revision(
        application=target_application,
        expected_revision=target_revision,
        model=model,
        timeout=timedelta(minutes=5),
    )
    juju_client.idle_for_period(model=model, timeout=timedelta(minutes=15))

    # Verify the application is upgraded to the target revision and the model is healthy
    upgraded_revision = juju_client.application_revision(application=target_application, model=model)
    if upgraded_revision != target_revision:
        pytest.fail(
            f"Expected '{target_application}' to be on upgraded revision "
            f"{target_revision}, got {upgraded_revision}."
        )
    juju_client.validate_model(model=model, level="simple")
