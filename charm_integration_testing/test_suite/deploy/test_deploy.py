# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.


from datetime import timedelta

import pytest
from juju import JujuClient


@pytest.mark.timeout(timedelta(minutes=15).total_seconds())
def test_deploy(
    juju_client: JujuClient,
    model: str,
    bundles: list[str],
    integrations: list[tuple[tuple[str, str], tuple[str, str]]],
):
    # Deploy each bundle
    for bundle in bundles:
        juju_client.deploy_bundle_file(bundle, model=model)

    # Create additional integrations
    for integration in integrations:
        juju_client.integrate(
            application_1=integration[0][0],
            endpoint_1=integration[0][1],
            application_2=integration[1][0],
            endpoint_2=integration[1][1],
            model=model,
        )

    # Wait until idle
    juju_client.idle_for_period(model=model)
