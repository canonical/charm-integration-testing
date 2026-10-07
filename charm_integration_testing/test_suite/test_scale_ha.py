# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta
from pathlib import Path

import pytest
from juju import JujuClient, JujuModelHandle

from bundle_builder_x import Charm

from .ha_helpers import bundle_application_units, require_principal_charm
from .scheduler.states import State


@pytest.mark.state(requires=State.DEPLOYED, provides=State.DEPLOYED_HA)
def test_scale_to_ha(
    juju_client: JujuClient,
    target_model_ref: JujuModelHandle,
    target_application: str,
    target_deployed_charm: Charm | None,
) -> None:
    charm = require_principal_charm(target_deployed_charm)
    ha_units = charm.ha_units
    current_units = juju_client.num_units(target_application, model=target_model_ref)
    if current_units < ha_units:
        juju_client.scale_application(target_application, ha_units, model=target_model_ref)
        juju_client.idle_for_period(model=target_model_ref, timeout=timedelta(minutes=15))

    juju_client.validate_model(model=target_model_ref, level="deep")


@pytest.mark.state(requires=State.DEPLOYED_HA, provides=State.DEPLOYED)
def test_scale_from_ha(
    juju_client: JujuClient,
    target_model_ref: JujuModelHandle,
    target_bundle: Path,
    target_application: str,
    target_platform: str,
    target_deployed_charm: Charm | None,
) -> None:
    charm = require_principal_charm(target_deployed_charm)
    if not charm.scale_down:
        pytest.skip(f"{charm.name} does not support scaling down from HA.")

    original_units = bundle_application_units(target_bundle, target_application, target_platform)
    juju_client.scale_application(target_application, original_units, model=target_model_ref)
    juju_client.idle_for_period(model=target_model_ref, timeout=timedelta(minutes=15))
    juju_client.validate_model(model=target_model_ref, level="simple")
