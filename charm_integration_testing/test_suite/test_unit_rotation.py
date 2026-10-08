# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import pytest
from juju import JujuClient, JujuModelHandle

from bundle_builder_x import Charm

from .fixtures.integration_spec import require_principal_charm
from .scheduler.states import State


@pytest.mark.state(requires=State.DEPLOYED_HA, provides=State.DEPLOYED_HA)
def test_unit_rotation(
    juju_client: JujuClient,
    target_model_ref: JujuModelHandle,
    neighbor_model_ref: JujuModelHandle | None,
    target_application: str,
    target_deployed_charm: Charm | None,
) -> None:
    charm = require_principal_charm(target_deployed_charm)
    if not charm.scale_down:
        pytest.skip(f"{charm.name} does not support scaling down from HA.")
    units = juju_client.application_units(target_application, model=target_model_ref)
    if len(units) < charm.ha_units:
        pytest.fail(
            f"Application {target_application} has {len(units)} units, fewer than its HA requirement "
            f"of {charm.ha_units}."
        )
    juju_client.rotate_application_units(
        target_application,
        target_model_ref,
        related_models=[neighbor_model_ref] if neighbor_model_ref is not None else None,
    )
