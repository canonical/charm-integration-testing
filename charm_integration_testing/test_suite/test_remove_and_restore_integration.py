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
    target_application: str,
    neighbor_model_ref: JujuModelHandle | None,
    neighbor_application: str,
    persistence_state: dict[PersistenceKey, PersistenceState],
) -> None:
    if not integration_endpoints_removable:
        pytest.skip(f"This integration is declared non-removable in {charm_overrides}.")

    # Removing and re-adding the relation gets it a brand new relation_id - in every model that
    # takes part in it. For a same-model integration that's just integration_model_ref; for a CMR,
    # both the offering model and the consuming model assign their own new relation_id, so any
    # tracked persistence state for this integration's units in *either* model is about to go
    # stale. Drop it now so the "prepare" call after re-adding seeds fresh canary data under the
    # new relation_id(s), rather than the checkpoints below failing with "relation id not found"
    # for stale entries.
    #
    # integration_endpoint_1/_2 can be a SAAS alias rather than the real application deployed in
    # a given model (see integration_spec.py): for a CMR, whichever side consumes the offer is
    # represented there by a local alias, not the remote application's real name, so resolving
    # affected units from those endpoints can silently miss the offering side. Use the real
    # target/neighbor application names instead - each is always deployed in its own model
    # (target_application in target_model_ref; neighbor_application in neighbor_model_ref, or in
    # target_model_ref itself for a same-model integration).
    neighbor_home = neighbor_model_ref if neighbor_model_ref is not None else target_model_ref
    model_applications: dict[JujuModelHandle, set[str]] = {}
    model_applications.setdefault(target_model_ref, set()).add(target_application)
    model_applications.setdefault(neighbor_home, set()).add(neighbor_application)

    candidate_models = {m for m in (target_model_ref, neighbor_model_ref) if m is not None}
    invalidated_models: set[JujuModelHandle] = set()
    for model_ref in candidate_models:
        affected_units: set[str] = set()
        for application in model_applications.get(model_ref, set()):
            affected_units.update(juju_backend.application_units(model_ref, application))
        if not affected_units:
            continue
        invalidated_models.add(model_ref)
        for key in [
            key
            for key in persistence_state
            if key.controller == model_ref.controller and key.model == model_ref.model and key.unit in affected_units
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

    # Validate all applications and relations in every involved model. Every model in
    # invalidated_models had its tracked state invalidated above (a CMR assigns a new relation_id
    # in *both* the offering and consuming model, not just integration_model_ref), so all of them
    # need a fresh "prepare" rather than "checkpoint" - otherwise the non-owning model's checkpoint
    # would run against an empty refs map and silently skip verifying its persistence validators.
    for model_ref in sorted_model_refs:
        persistence: Literal["prepare", "checkpoint"] = "prepare" if model_ref in invalidated_models else "checkpoint"
        juju_client.validate_model(
            model=model_ref, level="simple", persistence=persistence, persistence_state=persistence_state
        )
