# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.


from datetime import timedelta

from juju import JujuClient


def test_idempotent_redeploy(
    juju_client: JujuClient,
    model: str,
    applications: list[str],
    bundles: list[str],
) -> None:
    # Redeploy the bundle, which should redeploy the target application
    # existing applications will be ignored
    for bundle in bundles:
        juju_client.deploy_bundle_file(bundle, model=model)

    # Wait to become idle
    juju_client.idle_for_period(model=model, timeout=timedelta(minutes=15))

    # Assert applications are present after redeploy
    for application in applications:
        assert juju_client.application_exists(application, model=model)
