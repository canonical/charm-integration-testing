# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta

import pytest
from juju import JujuClient, JujuModelHandle

from bundle_builder_x import Charm

from .fixtures.integration_spec import require_principal_charm
from .scheduler.states import State

_TIMEOUT = timedelta(minutes=15)


def _wait_for_leader_recovery(
    juju_client: JujuClient,
    leader: str,
    model: JujuModelHandle,
    models: list[JujuModelHandle],
) -> None:
    juju_client.wait_for_unit_available(leader, model=model, timeout=_TIMEOUT)
    juju_client.multi_model_idle_for_period(models, timeout=_TIMEOUT)


@pytest.mark.state(requires=State.DEPLOYED_HA, provides=State.DEPLOYED_HA)
def test_reboot_leader(
    juju_client: JujuClient,
    target_model_ref: JujuModelHandle,
    target_application: str,
    target_deployed_charm: Charm | None,
    neighbor_model_ref: JujuModelHandle | None,
    neighbor_application: str | None,
) -> None:
    charm = require_principal_charm(target_deployed_charm)
    current_units = juju_client.num_units(target_application, model=target_model_ref)
    if current_units < charm.ha_units:
        pytest.fail(f"{target_application} has {current_units} units, fewer than its HA minimum of {charm.ha_units}.")

    leader = juju_client.application_leader(target_application, model=target_model_ref)
    models = [model for model in (target_model_ref, neighbor_model_ref) if model is not None]
    restart_attempted = False
    unit_unavailability_observed = False

    try:
        restart_attempted = True
        juju_client.reboot_unit(leader, model=target_model_ref)
        juju_client.wait_for_unit_unavailable(leader, model=target_model_ref, timeout=_TIMEOUT)
        unit_unavailability_observed = True
        juju_client.validate_model(model=target_model_ref, level="deep", applications=[target_application])
        if neighbor_application is not None:
            juju_client.validate_model(
                model=neighbor_model_ref or target_model_ref, level="deep", applications=[neighbor_application]
            )
    except Exception as operation_error:
        try:
            if restart_attempted and not unit_unavailability_observed:
                juju_client.wait_for_unit_unavailable(leader, model=target_model_ref, timeout=_TIMEOUT)
            _wait_for_leader_recovery(
                juju_client,
                leader,
                target_model_ref,
                models,
            )
        except Exception as recovery_error:
            raise operation_error from recovery_error
        raise
    else:
        _wait_for_leader_recovery(
            juju_client,
            leader,
            target_model_ref,
            models,
        )
