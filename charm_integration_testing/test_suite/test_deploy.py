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
    tmp_path: Path,
) -> None:
    target_model_uri = f"{target_controller}:{model}"
    all_bundles: list[tuple[Path, str]] = [(target_bundle, target_model_uri)]
    if neighbor_bundle is not None:
        assert neighbor_controller is not None
        assert neighbor_model is not None
        all_bundles.append((neighbor_bundle, f"{neighbor_controller}:{neighbor_model}"))

    juju_client.deploy_bundles(all_bundles, tmp_path)

    # TODO: Add multi-model wait
    # https://github.com/canonical/charm-integration-testing/issues/515
    for _, model_uri in all_bundles:
        juju_client.idle_for_period(model=model_uri, timeout=timedelta(minutes=15))

    for _, model_uri in all_bundles:
        juju_client.validate_model(model=model_uri, level="deep")
