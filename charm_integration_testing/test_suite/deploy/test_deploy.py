# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.


from datetime import timedelta

from juju import JujuClient


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
        if not juju_client.integration_exists(
            application_1=integration[0][0],
            endpoint_1=integration[0][1],
            application_2=integration[1][0],
            endpoint_2=integration[1][1],
            model=model,
        ):
            juju_client.integrate(
                application_1=integration[0][0],
                endpoint_1=integration[0][1],
                application_2=integration[1][0],
                endpoint_2=integration[1][1],
                model=model,
            )

    # Wait until idle
    juju_client.idle_for_period(model=model, timeout=timedelta(minutes=15))
