# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.


from datetime import timedelta

import pytest
from juju import JujuClient

from .scheduler.states import State


@pytest.mark.state(requires=State.NEIGHBOR_ONLY, provides=State.DEPLOYED)
def test_idempotent_redeploy(
    juju_client: JujuClient,
    model: str,
    target_application: str,
    bundles: list[str],
) -> None:
    # Redeploy the bundle, which should redeploy the target application
    # existing applications will be ignored
    for bundle in bundles:
        juju_client.deploy_bundle_file(bundle, model=model)

    # Wait to become idle
    juju_client.idle_for_period(model=model, timeout=timedelta(minutes=15))

    # Assert applications are present after redeploy
    assert juju_client.application_exists(
        target_application, model=model
    ), f"Application '{target_application}' was not found after redeploy"
