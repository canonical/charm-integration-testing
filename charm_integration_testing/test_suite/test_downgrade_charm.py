# Copyright 2025-2026 Canonical Ltd.
# See LICENSE file for licensing details.

import pytest
from juju import JujuClient, JujuModelHandle

from bundle_builder_x import Charm

from .charm_transitions import refresh_and_validate
from .scheduler.states import State


@pytest.mark.state(requires=State.DEPLOYED, provides=State.DEPLOYED_WITH_OLD_REVISION)
def test_downgrade_charm(
    juju_client: JujuClient,
    target_downgrade_charm: Charm,
    target_model_ref: JujuModelHandle,
    neighbor_model_ref: JujuModelHandle | None,
    target_application: str,
) -> None:
    refresh_and_validate(juju_client, target_application, target_downgrade_charm, target_model_ref, neighbor_model_ref)
