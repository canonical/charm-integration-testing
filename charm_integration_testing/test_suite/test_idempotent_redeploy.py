# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.


from datetime import timedelta
from pathlib import Path

import pytest
from juju import JujuClient

from .scheduler.states import State


@pytest.mark.state(requires=State.NEIGHBOR_ONLY, provides=State.DEPLOYED)
def test_idempotent_redeploy(
    juju_client: JujuClient,
    model: str,
    target_application: str,
    bundle: Path,
    all_bundles: list[tuple[Path, str]],
) -> None:
    if not len(all_bundles):
        all_bundles = [(bundle, model)]

    target_found = False
    for bundle, model_uri in all_bundles:
        _, controller, model = model_uri.split(":")
        model = f"{controller}:{model}"
        # Redeploy the bundle, which should redeploy the target application
        # existing applications will be ignored
        juju_client.deploy_bundle_file(str(bundle), model=model)

        # Wait to become idle
        juju_client.idle_for_period(model=model, timeout=timedelta(minutes=15))

        # Assert applications are present after redeploy
        target_found = juju_client.application_exists(target_application, model=model)

    assert target_found, f"Application '{target_application}' was not found after redeploy"

    for bundle, model_uri in all_bundles:
        _, controller, model = model_uri.split(":")
        model = f"{controller}:{model}"
        # Validate all applications and relations
        juju_client.validate_model(model=model, level="simple")
