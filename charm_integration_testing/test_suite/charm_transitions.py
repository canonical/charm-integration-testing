# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from juju import JujuApplicationInfo, JujuClient, JujuModelHandle

from bundle_builder_x import Charm


def refresh_and_validate(
    juju_client: JujuClient,
    application: str,
    charm: Charm,
    target_model: JujuModelHandle,
    neighbor_model: JujuModelHandle | None,
) -> None:
    """Refresh *application* to *charm*, then verify it runs that charm and the models are healthy."""
    replaced = juju_client.application_info(application, target_model)
    juju_client.logger.info(
        f"Refreshing {application} from {replaced.charm} revision {replaced.revision} to {charm.source_label}."
    )
    if charm.source_path is not None:
        juju_client.refresh_application_from_path(application, charm.source_path, model=target_model)
    else:
        assert charm.revision is not None
        juju_client.refresh_application(
            application, model=target_model, revision=charm.revision, channel=str(charm.channel)
        )
        juju_client.wait_for_application_revision(
            application=application,
            expected_revision=charm.revision,
            model=target_model,
            timeout=timedelta(minutes=5),
        )
    _settle_and_validate(juju_client, application, charm, target_model, neighbor_model, replaced)


def deploy_bundle_with_charm_and_validate(
    juju_client: JujuClient,
    bundle: Path,
    destination_bundle: Path,
    application: str,
    charm: Charm,
    target_model: JujuModelHandle,
    neighbor_model: JujuModelHandle | None,
) -> None:
    """Deploy *bundle* with *application* switched to *charm*, then verify it and the models are healthy."""
    with bundle.open("r", encoding="utf-8") as file:
        bundle_documents = list(yaml.safe_load_all(file))
    if not bundle_documents or not isinstance(bundle_documents[0], dict):
        raise ValueError(f"Invalid bundle file: {bundle}")
    bundle_data = bundle_documents[0]
    applications = bundle_data.get("applications")
    if not isinstance(applications, dict) or not isinstance(applications.get(application), dict):
        raise ValueError(f"Application '{application}' not found in bundle: {bundle}")
    application_data = applications[application]
    for source_field in ("charm", "channel", "revision"):
        application_data.pop(source_field, None)
    application_data.update(charm.bundle_source())
    with destination_bundle.open("w", encoding="utf-8") as file:
        yaml.safe_dump_all(bundle_documents, file, sort_keys=False)

    juju_client.logger.info(f"Deploying {application} from {charm.source_label}.")
    juju_client.deploy_bundle_file(str(destination_bundle), model=target_model)
    _settle_and_validate(juju_client, application, charm, target_model, neighbor_model, replaced=None)


def _settle_and_validate(
    juju_client: JujuClient,
    application: str,
    charm: Charm,
    target_model: JujuModelHandle,
    neighbor_model: JujuModelHandle | None,
    replaced: JujuApplicationInfo | None,
) -> None:
    models = [model for model in (target_model, neighbor_model) if model is not None]
    juju_client.multi_model_idle_for_period(models, timeout=timedelta(minutes=15))
    _assert_application_runs_charm(
        juju_client.application_info(application, target_model), application, charm, replaced
    )
    # For a CMR the persistence validator lives on the neighbor's requirer units, so validate there too.
    for model in models:
        juju_client.validate_model(model=model, level="simple")


def _assert_application_runs_charm(
    deployed: JujuApplicationInfo,
    application: str,
    charm: Charm,
    replaced: JujuApplicationInfo | None,
) -> None:
    if charm.source_path is not None:
        # Juju assigns local charm revisions on upload, so a new local revision proves this artifact was applied.
        reused_revision = replaced is not None and replaced.origin == "local" and replaced.revision == deployed.revision
        if deployed.origin != "local" or reused_revision:
            pytest.fail(
                f"Expected '{application}' to run a newly uploaded {charm.source_label}, "
                f"got {deployed.origin} charm {deployed.charm} revision {deployed.revision}."
            )
    elif deployed.origin not in (None, "charmhub") or deployed.revision != charm.revision:
        pytest.fail(
            f"Expected '{application}' to run {charm.source_label}, "
            f"got {deployed.origin} charm {deployed.charm} revision {deployed.revision}."
        )
