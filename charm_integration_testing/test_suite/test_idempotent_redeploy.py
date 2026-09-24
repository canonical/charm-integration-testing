# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.


from datetime import timedelta
from pathlib import Path

import pytest
from juju import JujuClient, JujuModelHandle

from .scheduler.states import State


@pytest.mark.state(requires=State.NEIGHBOR_ONLY, provides=State.DEPLOYED)
def test_idempotent_redeploy(
    juju_client: JujuClient,
    is_cmr_test: bool,
    target_application: str,
    target_bundle: Path,
    neighbor_bundle: Path | None,
    target_model_ref: JujuModelHandle,
    neighbor_model_ref: JujuModelHandle | None,
    tmp_path: Path,
) -> None:
    all_bundles: list[tuple[Path, JujuModelHandle]] = [(target_bundle, target_model_ref)]
    if is_cmr_test:
        assert neighbor_bundle is not None
        assert neighbor_model_ref is not None
        all_bundles.append((neighbor_bundle, neighbor_model_ref))

    juju_client.deploy_bundles(all_bundles, tmp_path)

    assert juju_client.application_exists(
        target_application, model=target_model_ref
    ), f"Application '{target_application}' was not found in target model after redeploy"

    for _, model_ref in all_bundles:
        juju_client.idle_for_period(model=model_ref, timeout=timedelta(minutes=15))

    for _, model_ref in all_bundles:
        juju_client.validate_model(model=model_ref, level="simple")
