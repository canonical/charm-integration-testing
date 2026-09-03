# Copyright 2024-2026 Canonical Ltd.
# See LICENSE file for licensing details.


from datetime import timedelta

import pytest
from juju import JujuClient, JujuIntegrationApplication, JujuModelHandle

from .scheduler.states import State


@pytest.mark.state(requires=State.DEPLOYED)
def test_remove_and_restore_integration(
    juju_client: JujuClient,
    integration_model_ref: JujuModelHandle,
    integration_endpoint_1: JujuIntegrationApplication,
    integration_endpoint_2: JujuIntegrationApplication,
    integration_endpoints_removable: bool,
    target_model_ref: JujuModelHandle,
    neighbor_model_ref: JujuModelHandle | None,
) -> None:
    if not integration_endpoints_removable:
        pytest.skip("This integration is declared non-removable in static/charm-overrides/.")

    # Break relation
    juju_client.remove_integration(
        model=integration_model_ref,
        endpoint_1=integration_endpoint_1,
        endpoint_2=integration_endpoint_2,
    )

    # Wait until integration is gone
    juju_client.wait_for_removal_of_integration(
        model=integration_model_ref,
        endpoint_1=integration_endpoint_1,
        endpoint_2=integration_endpoint_2,
        timeout=timedelta(minutes=10),
    )

    # Re-add integration
    juju_client.integrate(
        model=integration_model_ref,
        endpoint_1=integration_endpoint_1,
        endpoint_2=integration_endpoint_2,
    )

    # For CMR integrations, the provider side databag is populated by a unit agent that lives in
    # a different model to the one that owns the integration. Waiting for idle only on
    # `integration_model_ref` can race with that other model's agent still being "executing" when
    # validate_model runs, so wait for every model involved (target and, if present, neighbor)
    # to settle before validating.
    model_refs = {integration_model_ref, target_model_ref}
    if neighbor_model_ref is not None:
        model_refs.add(neighbor_model_ref)
    sorted_model_refs = sorted(model_refs, key=lambda m: m.uri)

    juju_client.multi_model_idle_for_period(sorted_model_refs, timeout=timedelta(minutes=15))

    # Validate all applications and relations in every involved model
    for model_ref in sorted_model_refs:
        juju_client.validate_model(model=model_ref, level="simple")
