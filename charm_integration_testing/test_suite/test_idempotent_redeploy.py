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
    is_cmr_test: bool,
    target_application: str,
    target_bundle: Path,
    neighbor_bundle: Path | None,
    model: str,
    target_controller: str,
    neighbor_model: str | None,
    neighbor_controller: str | None,
) -> None:
    target_model_uri = f"{target_controller}:{model}"
    bundles: dict[str, str] = {target_model_uri: str(target_bundle)}
    if is_cmr_test:
        assert neighbor_bundle is not None
        assert neighbor_controller is not None
        assert neighbor_model is not None
        bundles[f"{neighbor_controller}:{neighbor_model}"] = str(neighbor_bundle)

    # Redeploy bundles; existing applications will be skipped and config converged.
    juju_client.deploy_bundles(bundles)

    for model_uri in bundles:
        juju_client.idle_for_period(model=model_uri, timeout=timedelta(minutes=15))

    assert juju_client.application_exists(
        target_application, model=target_model_uri
    ), f"Application '{target_application}' was not found in target model after redeploy"

    for model_uri in bundles:
        juju_client.validate_model(model=model_uri, level="simple")
