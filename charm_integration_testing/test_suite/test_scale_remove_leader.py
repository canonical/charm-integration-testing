# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta

import pytest
from juju import JujuClient, JujuModelHandle

from bundle_builder_x import Charm

from .fixtures.integration_spec import require_principal_charm
from .scheduler.states import State


def _restore_application_scale(
    juju_client: JujuClient,
    application: str,
    leader: str,
    model: JujuModelHandle,
    models: list[JujuModelHandle],
    original_units: int,
    removal_attempted: bool,
    removal_request_returned: bool,
    removal_settled: bool,
) -> None:
    restore_scale = True
    if removal_attempted and not removal_settled:
        if removal_request_returned:
            juju_client.wait_for_unit_removal(leader, model=model, timeout=timedelta(minutes=15))
        else:
            juju_client.idle_for_period(model=model, timeout=timedelta(minutes=15))
            restore_scale = juju_client.num_units(application, model=model) != original_units
    if restore_scale:
        juju_client.scale_application(application, original_units, model=model)
    juju_client.multi_model_idle_for_period(models, timeout=timedelta(minutes=15))


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
    removal_attempted = False
    removal_request_returned = False
    removal_settled = False

    try:
        removal_attempted = True
        juju_client.remove_unit(leader, model=target_model_ref)
        removal_request_returned = True
        juju_client.wait_for_unit_removal(leader, model=target_model_ref, timeout=timedelta(minutes=15))
        removal_settled = True
        juju_client.idle_for_period(model=target_model_ref, timeout=timedelta(minutes=15))
        juju_client.validate_model(model=target_model_ref, level="deep", applications=[target_application])
        if neighbor_application is not None:
            juju_client.validate_model(
                model=neighbor_model_ref or target_model_ref, level="deep", applications=[neighbor_application]
            )
    except Exception as operation_error:
        try:
            _restore_application_scale(
                juju_client,
                target_application,
                leader,
                target_model_ref,
                models,
                original_units,
                removal_attempted,
                removal_request_returned,
                removal_settled,
            )
        except Exception as restoration_error:
            raise operation_error from restoration_error
        raise
    else:
        _restore_application_scale(
            juju_client,
            target_application,
            leader,
            target_model_ref,
            models,
            original_units,
            removal_attempted,
            removal_request_returned,
            removal_settled,
        )
