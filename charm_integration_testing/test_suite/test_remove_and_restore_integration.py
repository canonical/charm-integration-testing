# Copyright 2024-2026 Canonical Ltd.
# See LICENSE file for licensing details.


from datetime import timedelta
from pathlib import Path
from typing import Literal

import pytest
from juju import JujuBackend, JujuClient, JujuIntegrationApplication, JujuModelHandle, PersistenceKey

from validators.base import PersistenceState

from .scheduler.states import State


@pytest.mark.state(requires=State.DEPLOYED)
def test_remove_and_restore_integration(
    juju_client: JujuClient,
    juju_backend: JujuBackend,
    integration_model_ref: JujuModelHandle,
    integration_endpoint_1: JujuIntegrationApplication,
    integration_endpoint_2: JujuIntegrationApplication,
    integration_endpoints_removable: bool,
    charm_overrides: Path,
    target_model_ref: JujuModelHandle,
    neighbor_model_ref: JujuModelHandle | None,
    persistence_state: dict[PersistenceKey, PersistenceState],
) -> None:
    if not integration_endpoints_removable:
        pytest.skip(f"This integration is declared non-removable in {charm_overrides}.")

    # The relation being removed here gets a brand new relation_id when re-added below, so any
    # tracked persistence state for this integration's units is about to go stale. Drop it now so
    # the "prepare" call after re-adding seeds fresh canary data under the new relation_id, rather
    # than the checkpoint below failing to find a (now nonexistent) relation_id.
    #
    # For CMR integrations, integration_endpoint_1/_2 can be a SAAS alias rather than a real
    # application deployed in integration_model_ref (see integration_spec.py); application_units()
    # can't resolve a SAAS alias, so skip any endpoint that isn't actually deployed here.
    affected_units: set[str] = set()
    for endpoint in (integration_endpoint_1, integration_endpoint_2):
        if juju_client.application_exists(endpoint.application, model=integration_model_ref):
            affected_units.update(juju_backend.application_units(integration_model_ref, endpoint.application))
    for key in [
        key
        for key in persistence_state
        if key.controller == integration_model_ref.controller
        and key.model == integration_model_ref.model
        and key.unit in affected_units
    ]:
        del persistence_state[key]

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

    # Validate all applications and relations in every involved model. The integration's own model
    # gets a fresh "prepare" (its relation_id changed above); every other involved model is
    # verified with "checkpoint" as usual.
    for model_ref in sorted_model_refs:
        persistence: Literal["prepare", "checkpoint"] = (
            "prepare" if model_ref == integration_model_ref else "checkpoint"
        )
        juju_client.validate_model(
            model=model_ref, level="simple", persistence=persistence, persistence_state=persistence_state
        )
