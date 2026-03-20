# Copyright 2025-2026 Canonical Ltd.
# See LICENSE file for licensing details.

import time
from datetime import timedelta

import pytest
from juju import JujuClient

from .scheduler.states import State


def _application_revision(juju_client: JujuClient, model: str, application: str) -> int:
    applications = juju_client.list_applications(model=model)
    application_info = applications.get(application)
    if application_info is None:
        pytest.fail(f"Application '{application}' not found in model '{model}'")
    return application_info.revision


def _wait_for_application_revision(
    juju_client: JujuClient,
    model: str,
    application: str,
    expected_revision: int,
    timeout: timedelta = timedelta(minutes=15),
) -> None:
    """Wait until the application charm revision matches expected_revision.

    This prevents a race where the unit is still active/idle from the
    previous revision immediately after issuing juju refresh.
    """
    deadline = time.monotonic() + timeout.total_seconds()

    while time.monotonic() < deadline:
        actual_revision = _application_revision(juju_client=juju_client, model=model, application=application)
        if actual_revision == expected_revision:
            return
        time.sleep(1)

    pytest.fail(
        f"Timed out waiting for application '{application}' charm revision to be "
        f"{expected_revision}, got {actual_revision}"
    )


@pytest.mark.state(requires=State.OLD_REVISION)
def test_upgrade_charm(
    juju_client: JujuClient,
    model: str,
    target_application: str,
    target_revision: int | None,
    target_channel: str | None,
) -> None:
    if target_revision is None:
        pytest.fail("--target-revision must be provided as an integer for this test.")

    pre_upgrade_revision = _application_revision(juju_client=juju_client, model=model, application=target_application)
    if pre_upgrade_revision == target_revision:
        pytest.skip(
            f"Target application '{target_application}' is already on revision "
            f"{target_revision}; no upgrade/downgrade path to test."
        )

    juju_client.logger.info(
        f"Refreshing {target_application} from pre-upgrade revision "
        f"{pre_upgrade_revision} to original bundle revision {target_revision}."
    )
    juju_client.refresh_application(
        application=target_application,
        revision=target_revision,
        channel=target_channel,
        model=model,
    )
    _wait_for_application_revision(
        juju_client=juju_client,
        model=model,
        application=target_application,
        expected_revision=target_revision,
        timeout=timedelta(minutes=5),
    )
    juju_client.idle_for_period(model=model, timeout=timedelta(minutes=15))

    upgraded_revision = _application_revision(juju_client=juju_client, model=model, application=target_application)
    if upgraded_revision != target_revision:
        pytest.fail(
            f"Expected '{target_application}' to be on upgraded revision "
            f"{target_revision}, got {upgraded_revision}."
        )
    juju_client.validate_model(model=model, level="simple")

    juju_client.logger.info(f"Refreshing {target_application} back to pre-upgrade revision " f"{pre_upgrade_revision}.")
    juju_client.refresh_application(
        application=target_application,
        revision=pre_upgrade_revision,
        channel=target_channel,
        model=model,
    )
    _wait_for_application_revision(
        juju_client=juju_client,
        model=model,
        application=target_application,
        expected_revision=pre_upgrade_revision,
        timeout=timedelta(minutes=5),
    )
    juju_client.idle_for_period(model=model, timeout=timedelta(minutes=15))
    downgraded_revision = _application_revision(juju_client=juju_client, model=model, application=target_application)
    if downgraded_revision != pre_upgrade_revision:
        pytest.fail(
            f"Expected '{target_application}' to be on downgraded revision "
            f"{pre_upgrade_revision}, got {downgraded_revision}."
        )
    juju_client.validate_model(model=model, level="simple")
