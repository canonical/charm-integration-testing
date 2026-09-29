# Copyright 2024-2026 Canonical Ltd.
# See LICENSE file for licensing details.


from datetime import timedelta
from pathlib import Path

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
    charm_overrides: Path,
    target_model_ref: JujuModelHandle,
    target_application: str,
    neighbor_model_ref: JujuModelHandle | None,
    neighbor_application: str,
) -> None:
    if not integration_endpoints_removable:
        pytest.skip(f"This integration is declared non-removable in {charm_overrides}.")

    # Re-adding the relation assigns a brand new relation_id in every model taking part in it
    # (both sides, for a CMR), so the extension's pre_remove_integration hook drops the tracked
    # canary state for this integration before the relation is removed below; the re-add then
    # seeds fresh canary data under the new relation_id on the next validate.

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

    # For CMR integrations the provider-side databag is populated by an agent in a different
    # model, so wait for every involved model to settle before validating.
    model_refs = {integration_model_ref, target_model_ref}
    if neighbor_model_ref is not None:
        model_refs.add(neighbor_model_ref)
    sorted_model_refs = sorted(model_refs, key=lambda m: m.uri)

    juju_client.multi_model_idle_for_period(sorted_model_refs, timeout=timedelta(minutes=15))

    # The pre_remove_integration hook cleaned up the affected models' canary state, so each
    # validate auto-decides "prepare" and seeds fresh canary data under the new relation_id.
    for model_ref in sorted_model_refs:
        juju_client.validate_model(model=model_ref, level="simple")
