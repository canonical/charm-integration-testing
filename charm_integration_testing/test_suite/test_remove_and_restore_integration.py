# Copyright 2024-2026 Canonical Ltd.
# See LICENSE file for licensing details.


from datetime import timedelta
from pathlib import Path
from typing import Literal

import pytest
from extensions import ValidatorInjectorExtension
from juju import JujuBackend, JujuClient, JujuIntegrationApplication, JujuModelHandle

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
    persistence_extension: ValidatorInjectorExtension,
) -> None:
    if not integration_endpoints_removable:
        pytest.skip(f"This integration is declared non-removable in {charm_overrides}.")

    # Re-adding the relation assigns a brand new relation_id in every model taking part in it
    # (both sides, for a CMR), so tracked persistence state for this integration's units is about
    # to go stale. Resolve affected units from the real target/neighbor application names rather
    # than integration_endpoint_1/_2, which can be a SAAS alias for a CMR and so would miss the
    # offering side.
    neighbor_home = neighbor_model_ref if neighbor_model_ref is not None else target_model_ref
    model_applications: dict[JujuModelHandle, set[str]] = {}
    model_applications.setdefault(target_model_ref, set()).add(target_application)
    model_applications.setdefault(neighbor_home, set()).add(neighbor_application)

    candidate_models = {m for m in (target_model_ref, neighbor_model_ref) if m is not None}
    invalidated_models: set[JujuModelHandle] = set()
    # Compute which tracked entries will go stale, but don't drop them yet: if the remove/re-add
    # below fails, the old relation may still be present and teardown still needs these entries to
    # retry cleanup() for them.
    units_by_model: dict[JujuModelHandle, set[str]] = {}
    for model_ref in candidate_models:
        affected_units: set[str] = set()
        for application in model_applications.get(model_ref, set()):
            affected_units.update(juju_backend.application_units(model_ref, application))
        if not affected_units:
            continue
        invalidated_models.add(model_ref)
        units_by_model[model_ref] = affected_units

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

    # The remove/re-add succeeded, so the tracked entries for the affected units are now
    # genuinely stale.
    persistence_extension.invalidate_persistence_state_for_models(invalidated_models, units_by_model)

    # For CMR integrations the provider-side databag is populated by an agent in a different
    # model, so wait for every involved model to settle before validating.
    model_refs = {integration_model_ref, target_model_ref}
    if neighbor_model_ref is not None:
        model_refs.add(neighbor_model_ref)
    sorted_model_refs = sorted(model_refs, key=lambda m: m.uri)

    juju_client.multi_model_idle_for_period(sorted_model_refs, timeout=timedelta(minutes=15))

    # Every model in invalidated_models had its tracked state dropped above, so it needs a fresh
    # "prepare" rather than a "checkpoint" (which would run against an empty refs map and silently
    # skip verifying its persistence validators).
    for model_ref in sorted_model_refs:
        persistence: Literal["prepare", "checkpoint"] = "prepare" if model_ref in invalidated_models else "checkpoint"
        juju_client.validate_model(model=model_ref, level="simple", persistence=persistence)
