# Copyright 2025-2026 Canonical Ltd.
# See LICENSE file for licensing details.


from datetime import timedelta

import pytest
from extensions import ValidatorInjectorExtension
from juju import JujuClient, JujuIntegrationApplication, JujuModelHandle, JujuValidationError

from validators.base import ValidationResult

from .scheduler.states import State


@pytest.mark.state(requires=[State.DEPLOYED, State.DEPLOYED_HA], provides=State.NEIGHBOR_ONLY)
def test_teardown(
    juju_client: JujuClient,
    target_model_ref: JujuModelHandle,
    target_application: str,
    is_cmr_integration: bool,
    integration_model_ref: JujuModelHandle,
    integration_endpoint_1: JujuIntegrationApplication,
    integration_endpoint_2: JujuIntegrationApplication,
    consumed_offer_alias: str | None,
    neighbor_model_ref: JujuModelHandle | None,
    persistence_extension: ValidatorInjectorExtension,
) -> None:
    # Drop all canary data before the applications hosting it are torn down: cleanup() runs on the
    # units themselves, so it must happen while those units still exist. Clean up every model the
    # extension still holds state for (test_deploy prepares every model in all_bundles, including
    # the neighbor model for CMR runs).
    cleanup_model_refs = persistence_extension.models_with_persistence_state
    # Best-effort across models: validate_model() raises JujuValidationError on a FAIL/ERROR result,
    # but application/model-discovery failures (e.g. the application is already gone) surface as a
    # bare exception. Attempt every model, merge JujuValidationError failures, and remember the
    # first other exception so one model's failure doesn't skip cleanup for the rest.
    combined_failed_validations: dict[str, list[ValidationResult]] = {}
    first_other_error: Exception | None = None
    for model_ref in sorted(cleanup_model_refs, key=lambda m: m.uri):
        try:
            juju_client.validate_model(model=model_ref, level=None, persistence="cleanup")
        except JujuValidationError as exc:
            for unit, results in exc.failed_validations.items():
                combined_failed_validations.setdefault(unit, []).extend(results)
        except Exception as exc:  # broad on purpose - see comment above
            if first_other_error is None:
                first_other_error = exc
    if combined_failed_validations:
        # Chain first_other_error as the cause rather than discarding it, so a transport failure on
        # one model isn't invisible when another model also failed validation.
        raise JujuValidationError(combined_failed_validations) from first_other_error
    if first_other_error is not None:
        raise first_other_error

    # Juju refuses to destroy an application whose offer still has a connected consumer
    # ("used by N consumer(s)"). For CMR integrations the consumer lives in whichever model is
    # consuming (target or neighbor, depending on the integration), so the relation has to be torn
    # down from the consuming side first; nothing else removes it on its own. See
    # https://github.com/canonical/charm-integration-testing/issues/939.
    # Same-model relations don't hit this: destroying the application removes them too.
    if is_cmr_integration:
        juju_client.remove_integration(
            model=integration_model_ref,
            endpoint_1=integration_endpoint_1,
            endpoint_2=integration_endpoint_2,
        )
        juju_client.wait_for_removal_of_integration(
            model=integration_model_ref,
            endpoint_1=integration_endpoint_1,
            endpoint_2=integration_endpoint_2,
            timeout=timedelta(minutes=10),
        )

    # Remove all requested applications
    juju_client.remove_applications(target_application, model=target_model_ref)

    # Wait until application has been removed
    juju_client.wait_for_removal(target_application, model=target_model_ref, timeout=timedelta(minutes=15))

    # For CMR integrations, removing the offer doesn't clean up the consuming side's SAAS proxy:
    # juju status keeps listing it indefinitely (eventually as "dead"/"terminated") since nothing
    # removes it automatically. That blocks a later redeploy under the same SAAS alias (e.g.
    # test_idempotent_redeploy) with "exists but is terminating". Remove it explicitly rather than
    # waiting for a status change that may never happen on its own.
    if is_cmr_integration:
        assert consumed_offer_alias is not None
        juju_client.remove_saas(consumed_offer_alias, model=integration_model_ref)
        # Removal is asynchronous on the controller; a later re-consumption of the same alias
        # (e.g. a redeploy) can otherwise race with the still-in-progress teardown.
        # See https://github.com/canonical/charm-integration-testing/issues/1045.
        juju_client.wait_for_removal_of_saas(
            consumed_offer_alias, model=integration_model_ref, timeout=timedelta(minutes=10)
        )
