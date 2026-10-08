# Copyright 2025-2026 Canonical Ltd.
# See LICENSE file for licensing details.

from pathlib import Path

import pytest
from juju import JujuClient, JujuModelHandle

from bundle_builder_x import Charm

from .charm_transitions import deploy_bundle_with_charm_and_validate
from .scheduler.states import State


@pytest.mark.state(requires=State.NEIGHBOR_ONLY, provides=State.DEPLOYED_WITH_OLD_REVISION)
def test_deploy_target_old_revision(
    juju_client: JujuClient,
    target_downgrade_charm: Charm,
    target_model_ref: JujuModelHandle,
    neighbor_model_ref: JujuModelHandle | None,
    target_application: str,
    tmp_path: Path,
    target_bundle: Path,
) -> None:
    # Validation seeds canary data for later persistence checks.
    deploy_bundle_with_charm_and_validate(
        juju_client,
        bundle=target_bundle,
        destination_bundle=tmp_path / f"bundle-{target_application}-downgrade.yaml",
        application=target_application,
        charm=target_downgrade_charm,
        target_model=target_model_ref,
        neighbor_model=neighbor_model_ref,
    )
