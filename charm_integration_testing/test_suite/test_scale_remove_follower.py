# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta

import pytest
from juju import JujuClient, JujuModelHandle, JujuRestartNotSupportedError

from bundle_builder_x import Charm

from .scheduler.states import State
from .test_scale_ha import _require_principal_charm


@pytest.mark.state(requires=State.DEPLOYED_HA, provides=State.DEPLOYED_HA)
def test_scale_remove_follower(
    juju_client: JujuClient,
    target_model_ref: JujuModelHandle,
    neighbor_model_ref: JujuModelHandle | None,
    target_application: str,
    target_deployed_charm: Charm | None,
) -> None:
    charm = _require_principal_charm(target_deployed_charm)
    timeout = timedelta(minutes=15)
    models = list(dict.fromkeys([target_model_ref, *([neighbor_model_ref] if neighbor_model_ref else [])]))
    juju_client.multi_model_idle_for_period(models=models, timeout=timeout, strict_timeout=True)
    original_units = juju_client.num_units(target_application, model=target_model_ref)
    if original_units < charm.ha_units:
        pytest.fail(f"Expected at least {charm.ha_units} HA units, found {original_units}.")
    if original_units < 2:
        pytest.skip("The application has no follower to restart.")

    try:
        juju_client.restart_follower(target_application, model=target_model_ref, timeout=timeout)
    except JujuRestartNotSupportedError as error:
        pytest.skip(str(error))

    juju_client.multi_model_idle_for_period(models=models, timeout=timeout, strict_timeout=True)
    if juju_client.num_units(target_application, model=target_model_ref) != original_units:
        pytest.fail("The application unit count changed after restarting its follower.")
    for model in models:
        juju_client.validate_model(model=model, level="simple")
