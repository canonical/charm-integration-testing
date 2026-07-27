# Copyright 2024-2026 Canonical Ltd.
# See LICENSE file for licensing details.


from datetime import timedelta
from pathlib import Path

import pytest
from juju import JujuClient, JujuIntegrationApplication

from ._idle_timeouts import idle_timeout_for_bundles
from .scheduler.states import State


@pytest.mark.state(requires=State.DEPLOYED)
def test_remove_and_restore_integration(
    juju_client: JujuClient,
    integration_controller: str,
    integration_model: str,
    integration_endpoint_1: JujuIntegrationApplication,
    integration_endpoint_2: JujuIntegrationApplication,
    target_bundle: Path,
    neighbor_bundle: Path | None,
) -> None:
    model_uri = f"{integration_controller}:{integration_model}"

    # Break relation
    juju_client.remove_integration(
        model=model_uri,
        endpoint_1=integration_endpoint_1,
        endpoint_2=integration_endpoint_2,
    )

    # Wait until integration is gone
    juju_client.wait_for_removal_of_integration(
        model=model_uri,
        endpoint_1=integration_endpoint_1,
        endpoint_2=integration_endpoint_2,
        timeout=timedelta(minutes=10),
    )

    # Re-add integration
    juju_client.integrate(
        model=model_uri,
        endpoint_1=integration_endpoint_1,
        endpoint_2=integration_endpoint_2,
    )

    # Wait to become idle
    # See https://github.com/canonical/charm-integration-testing/issues/794: some charms
    # (e.g. postgresql-k8s/postgresql) can take materially longer than the default window
    # to re-settle relation data after being re-integrated, independent of any charm bug.
    juju_client.idle_for_period(model=model_uri, timeout=idle_timeout_for_bundles([target_bundle, neighbor_bundle]))

    # Validate all applications and relations
    juju_client.validate_model(model=model_uri, level="simple")
