# Copyright 2025-2026 Canonical Ltd.
# See LICENSE file for licensing details.


from datetime import timedelta

import pytest
from juju import JujuClient, JujuIntegrationApplication, JujuModelHandle

from .scheduler.states import State


@pytest.mark.state(requires=State.DEPLOYED, provides=State.NEIGHBOR_ONLY)
def test_teardown(
    juju_client: JujuClient,
    target_model_ref: JujuModelHandle,
    target_application: str,
    is_cmr_integration: bool,
    integration_model_ref: JujuModelHandle,
    integration_endpoint_1: JujuIntegrationApplication,
    integration_endpoint_2: JujuIntegrationApplication,
    consumed_offer_alias: str | None,
) -> None:
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
