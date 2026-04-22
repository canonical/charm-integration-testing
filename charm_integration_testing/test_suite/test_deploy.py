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
    # FIXME(@motjuste): we should just use all_bundles
    model: str,
    bundle: Path,
    all_bundles: list[tuple[Path, str]],
) -> None:
    if not len(all_bundles):
        all_bundles = [(bundle, model)]

    # Deploy all bundles
    for bundle_path, model_uri in all_bundles:  # IDEA(@motjuste): could be parallelised
        juju_client.deploy_bundle_file(str(bundle_path), model=model_uri)

        # Wait until all idle
        juju_client.idle_for_period(model=model_uri, timeout=timedelta(minutes=15))

    # Validate all applications and relations
    for _, model_uri in all_bundles:
        juju_client.validate_model(model=model_uri, level="deep")
