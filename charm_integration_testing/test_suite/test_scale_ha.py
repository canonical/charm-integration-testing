# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from juju import JujuClient, JujuModelHandle, JujuRestartNotSupportedError

from bundle_builder_x import Charm

from .scheduler.states import State


def _require_principal_charm(charm: Charm | None) -> Charm:
    if charm is None:
        pytest.fail("Unable to resolve the deployed target charm metadata needed for HA scaling.")
    if charm.subordinate:
        pytest.skip(f"{charm.name} is subordinate and cannot be scaled independently.")
    return charm


def _bundle_application_units(bundle_path: Path, application: str, platform: str) -> int:
    with bundle_path.open(encoding="utf-8") as file:
        try:
            bundle = next(yaml.safe_load_all(file))
        except StopIteration:
            raise ValueError(f"Bundle is empty: {bundle_path}") from None

    if not isinstance(bundle, dict):
        raise ValueError(f"Invalid bundle document in {bundle_path}.")
    applications = bundle.get("applications")
    if not isinstance(applications, dict) or application not in applications:
        raise ValueError(f"Application '{application}' not found in bundle: {bundle_path}")
    application_data = applications[application]
    if not isinstance(application_data, dict):
        raise ValueError(f"Invalid application definition for '{application}' in {bundle_path}.")

    units_key = "scale" if platform == "kubernetes" else "num_units"
    units = application_data.get(units_key)
    if isinstance(units, bool) or not isinstance(units, int) or units < 1:
        raise ValueError(f"Application '{application}' in {bundle_path} must define a positive integer '{units_key}'.")
    return units


@pytest.mark.state(requires=State.DEPLOYED, provides=State.DEPLOYED_HA)
def test_scale_to_ha(
    juju_client: JujuClient,
    target_model_ref: JujuModelHandle,
    target_application: str,
    target_deployed_charm: Charm | None,
) -> None:
    charm = _require_principal_charm(target_deployed_charm)
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
    charm = _require_principal_charm(target_deployed_charm)
    if not charm.scale_down:
        pytest.skip(f"{charm.name} does not support scaling down from HA.")

    original_units = _bundle_application_units(target_bundle, target_application, target_platform)
    juju_client.scale_application(target_application, original_units, model=target_model_ref)
    juju_client.idle_for_period(model=target_model_ref, timeout=timedelta(minutes=15))
    juju_client.validate_model(model=target_model_ref, level="simple")


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
