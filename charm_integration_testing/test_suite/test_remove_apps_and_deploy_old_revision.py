# Copyright 2025-2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from juju import JujuClient

from .scheduler.states import State


def _create_bundle_with_revision_override(
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


@pytest.mark.state(requires=State.NEIGHBOR_ONLY, provides=State.DEPLOYED_WITH_OLD_REVISION)
def test_deploy(
    juju_client: JujuClient,
    historical_revision_with_passing_deploy: int | None,
    model: str,
    bundle: Path,
    target_application: str,
    target_charm: str,
    target_channel: str | None,
    target_revision: int | None,
    tmp_path: Path,
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

    ### Create a temporary bundle file with target revision overridden
    ### Should be replaced by bundle-builder with old revision in the future
    overridden_bundle = tmp_path / f"bundle-{target_application}-rev-{selected_revision}.yaml"
    _create_bundle_with_revision_override(
        source_bundle=bundle,
        destination_bundle=overridden_bundle,
        target_application=target_application,
        target_revision=selected_revision,
    )

    # Deploy the original bundle with only the target app revision overridden
    juju_client.deploy_bundle_file(str(overridden_bundle), model=model)

    # Wait until idle
    juju_client.idle_for_period(model=model, timeout=timedelta(minutes=15))

    # Verify the application is deployed at the target revision and the model is healthy
    deployed_revision = juju_client.application_revision(application=target_application, model=model)
    if deployed_revision != selected_revision:
        pytest.fail(
            f"Expected '{target_application}' to be deployed at revision {selected_revision}, "
            f"got {deployed_revision}."
        )

    # Validate all applications and relations
    juju_client.validate_model(model=model, level="simple")
