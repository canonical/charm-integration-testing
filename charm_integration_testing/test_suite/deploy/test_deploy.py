# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.


from datetime import timedelta

from juju import JujuClient


def test_deploy(
    juju_client: JujuClient,
    model: str,
    bundles: list[str],
) -> None:
    # Deploy each bundle
    for bundle in bundles:
        juju_client.deploy_bundle_file(bundle, model=model)

    # Wait until idle
    juju_client.idle_for_period(model=model, timeout=timedelta(minutes=15))
