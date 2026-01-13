# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.


from datetime import timedelta

from juju import JujuClient


def test_teardown(juju_client: JujuClient, model: str, applications: list[str]) -> None:
    # Remove all requested applications
    juju_client.remove_applications(*applications, model=model)

    # Wait until application has been removed
    juju_client.wait_for_removal(*applications, model=model, timeout=timedelta(minutes=15))
