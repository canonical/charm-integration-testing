# Copyright 2025-2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta
from pathlib import Path
import time
from typing import Any

import pytest
import yaml
from juju import JujuClient

from .scheduler.states import State


def _load_target_from_bundle(bundle: Path, target_application: str) -> dict[str, Any]:
    with bundle.open("r", encoding="utf-8") as file:
        bundle_data = yaml.safe_load(file)
    if not isinstance(bundle_data, dict):
        pytest.fail(f"Invalid bundle format in {bundle}")
    applications = bundle_data.get("applications")
    if not isinstance(applications, dict) or target_application not in applications:
        pytest.fail(f"Application '{target_application}' not found in bundle {bundle}")
    target_data = applications[target_application]
    if not isinstance(target_data, dict):
        pytest.fail(f"Invalid application entry for '{target_application}' in bundle {bundle}")
    return target_data


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
        actual_revision = _application_revision(
            juju_client=juju_client, model=model, application=application
        )
        if actual_revision == expected_revision:
            return
        time.sleep(1)

    pytest.fail(
        f"Timed out waiting for application '{application}' charm revision to be "
        f"{expected_revision}, got {actual_revision}"
    )


@pytest.mark.state(requires=State.OLD_REVISION, provides=State.OLD_REVISION)
def test_upgrade_charm(
    juju_client: JujuClient,
    model: str,
    bundle: Path,
    target_application: str,
) -> None:
    target_data = _load_target_from_bundle(bundle=bundle, target_application=target_application)

    original_revision = target_data.get("revision")
    if not isinstance(original_revision, int):
        pytest.fail(
            f"Application '{target_application}' is missing an integer "
            f"revision in bundle {bundle}"
        )
    target_channel = (
        target_data.get("channel")
        if isinstance(target_data.get("channel"), str)
        else None
    )

    pre_upgrade_revision = _application_revision(
        juju_client=juju_client, model=model, application=target_application
    )
    if pre_upgrade_revision == original_revision:
        pytest.skip(
            f"Target application '{target_application}' is already on revision "
            f"{original_revision}; no upgrade/downgrade path to test."
        )

    juju_client.logger.info(
        f"Refreshing {target_application} from pre-upgrade revision "
        f"{pre_upgrade_revision} to original bundle revision {original_revision}."
    )
    juju_client.refresh_application(
        application=target_application,
        revision=original_revision,
        channel=target_channel,
        model=model,
    )
    _wait_for_application_revision(
        juju_client=juju_client,
        model=model,
        application=target_application,
        expected_revision=original_revision,
        timeout=timedelta(minutes=5),
    )
    juju_client.idle_for_period(model=model, timeout=timedelta(minutes=15))
    
    upgraded_revision = _application_revision(
        juju_client=juju_client, model=model, application=target_application
    )
    if upgraded_revision != original_revision:
        pytest.fail(
            f"Expected '{target_application}' to be on upgraded revision "
            f"{original_revision}, got {upgraded_revision}."
        )
    juju_client.validate_model(model=model, level="simple")

    juju_client.logger.info(
        f"Refreshing {target_application} back to pre-upgrade revision "
        f"{pre_upgrade_revision}."
    )
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
    downgraded_revision = _application_revision(
        juju_client=juju_client, model=model, application=target_application
    )
    if downgraded_revision != pre_upgrade_revision:
        pytest.fail(
            f"Expected '{target_application}' to be on downgraded revision "
            f"{pre_upgrade_revision}, got {downgraded_revision}."
        )
    juju_client.validate_model(model=model, level="simple")
