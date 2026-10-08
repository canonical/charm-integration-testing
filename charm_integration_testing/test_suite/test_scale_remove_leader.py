# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta

import pytest
from juju import JujuClient, JujuModelHandle

from bundle_builder_x import Charm

from .ha_utils import require_principal_charm
from .scheduler.states import State


@pytest.mark.state(requires=State.DEPLOYED_HA, provides=State.DEPLOYED_HA)
def test_scale_remove_leader(
    juju_client: JujuClient,
    target_model_ref: JujuModelHandle,
    target_application: str,
    target_platform: str,
    target_deployed_charm: Charm | None,
    neighbor_model_ref: JujuModelHandle | None,
    neighbor_application: str | None,
) -> None:
    if target_platform == "kubernetes":
        pytest.skip("Juju does not support removing a specific unit from a Kubernetes model.")

    charm = require_principal_charm(target_deployed_charm)
    if not charm.scale_down:
        pytest.skip(f"{charm.name} does not support scaling down from HA.")

    original_units = juju_client.num_units(target_application, model=target_model_ref)
    if original_units < charm.ha_units:
        pytest.fail(f"{target_application} has {original_units} units, fewer than its HA minimum of {charm.ha_units}.")

    leader = juju_client.application_leader(target_application, model=target_model_ref)
    models = [model for model in (target_model_ref, neighbor_model_ref) if model is not None]

    def restore_application() -> None:
        current_units = juju_client.num_units(target_application, model=target_model_ref)
        if current_units != original_units:
            juju_client.scale_application(target_application, original_units, model=target_model_ref)
        juju_client.multi_model_idle_for_period(models, timeout=timedelta(minutes=15))

    try:
        juju_client.remove_unit(leader, model=target_model_ref)
        juju_client.wait_for_unit_removal(leader, model=target_model_ref, timeout=timedelta(minutes=15))
        juju_client.idle_for_period(model=target_model_ref, timeout=timedelta(minutes=15))
        juju_client.validate_model(model=target_model_ref, level="deep", applications=[target_application])
        if neighbor_application is not None:
            juju_client.validate_model(
                model=neighbor_model_ref or target_model_ref, level="deep", applications=[neighbor_application]
            )
    except Exception as operation_error:
        try:
            restore_application()
        except Exception as restoration_error:
            raise operation_error from restoration_error
        raise
    else:
        restore_application()
