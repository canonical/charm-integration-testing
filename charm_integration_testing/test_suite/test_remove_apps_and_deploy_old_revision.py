# Copyright 2025-2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from juju import JujuClient

from .conftest import TestObserverClient
from .scheduler.states import State


def _extract_track_and_stage(channel: str | None) -> tuple[str, str]:
    if not channel:
        return "14", "stable"
    # Channels are usually track/risk, e.g. "latest/edge".
    return channel.split("/")[0], channel.split("/")[1]


def create_bundle_with_revision_override(
    source_bundle: Path,
    destination_bundle: Path,
    target_application: str,
    target_revision: int,
) -> None:
    with source_bundle.open("r", encoding="utf-8") as file:
        bundle_data = yaml.safe_load(file)

    if not isinstance(bundle_data, dict):
        raise ValueError(f"Invalid bundle file: {source_bundle}")

    applications = bundle_data.get("applications")
    if not isinstance(applications, dict) or target_application not in applications:
        raise ValueError(f"Application '{target_application}' not found in bundle: {source_bundle}")

    target_application_data = applications[target_application]
    if not isinstance(target_application_data, dict):
        raise ValueError(f"Invalid application definition for '{target_application}' in {source_bundle}")

    target_application_data["revision"] = target_revision

    with destination_bundle.open("w", encoding="utf-8") as file:
        yaml.safe_dump(bundle_data, file, sort_keys=False)


@pytest.mark.state(requires=State.DEPLOYED, provides=State.OLD_REVISION)
def test_deploy(
    juju_client: JujuClient,
    test_observer_client: TestObserverClient,
    model: str,
    bundle: Path,
    target_application: str,
    target_charm: str,
    target_channel: str | None,
    target_revision: int | None,
    tmp_path: Path,
) -> None:
    if not test_observer_client.enabled:
        pytest.skip("TEST_OBSERVER_API is required to query artefacts history")

    if target_revision is None:
        pytest.fail("--target-revision must be provided as an integer for this test.")

    # Extract stage and track from --target-channel passed by charm-testing.yaml.
    track, stage = _extract_track_and_stage(target_channel)

    # Get all applications from the model
    apps = list(juju_client.list_applications(model=model).keys())

    # Remove all applications from the model
    if apps:
        juju_client.remove_applications(*apps, model=model)
        juju_client.wait_for_removal(*apps, model=model, timeout=timedelta(minutes=15))

    # Query Test Observer for historical revisions and pick the first one with a passing test_deploy run.
    selected_revision = test_observer_client.choose_historical_revision_with_passing_deploy(
        charm_name=target_charm,
        stage=stage,
        current_revision=target_revision,
        track=track,
    )
    if selected_revision is None:
        pytest.fail(
            "Unable to find a historical revision with a passing test_deploy result "
            f"for charm '{target_charm}' in stage '{stage}'."
        )

    juju_client.logger.info(
        f"Selected historical revision {selected_revision} for {target_application} ({target_charm})"
    )

    overridden_bundle = tmp_path / f"bundle-{target_application}-rev-{selected_revision}.yaml"
    create_bundle_with_revision_override(
        source_bundle=bundle,
        destination_bundle=overridden_bundle,
        target_application=target_application,
        target_revision=selected_revision,
    )

    # Deploy the original bundle with only the target app revision overridden.
    juju_client.deploy_bundle_file(str(overridden_bundle), model=model)

    # Wait until idle
    juju_client.idle_for_period(model=model, timeout=timedelta(minutes=15))

    # Validate all applications and relations
    juju_client.validate_model(model=model, level="simple")
