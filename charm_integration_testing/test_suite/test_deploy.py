# Copyright 2025-2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta
from pathlib import Path

import pytest
from juju import JujuClient

from .scheduler.states import State


@pytest.mark.state(requires=State.EMPTY_MODEL, provides=State.DEPLOYED)
def test_deploy(
    juju_client: JujuClient,
    target_bundle: Path,
    neighbor_bundle: Path | None,
    model: str,
    target_controller: str,
    neighbor_model: str | None,
    neighbor_controller: str | None,
) -> None:
    target_model_uri = f"{target_controller}:{model}"
    bundles: dict[str, str] = {target_model_uri: str(target_bundle)}
    if neighbor_bundle is not None:
        assert neighbor_controller is not None
        assert neighbor_model is not None
        bundles[f"{neighbor_controller}:{neighbor_model}"] = str(neighbor_bundle)

    juju_client.deploy_bundles(bundles)

    # TODO: Add multi-model wait
    # https://github.com/canonical/charm-integration-testing/issues/515
    for model_uri in bundles:
        juju_client.idle_for_period(model=model_uri, timeout=timedelta(minutes=15))

    for model_uri in bundles:
        juju_client.validate_model(model=model_uri, level="deep")
