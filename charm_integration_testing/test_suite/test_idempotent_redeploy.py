# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Verify that the bundle can be redeployed after the target application is removed.

This is a *transition test*: it moves the environment from ``neighbor_only``
back to ``deployed``.  Redeploying on top of a partially-populated model
validates that the bundle is truly idempotent — existing applications are
left untouched while missing ones are restored.
"""

from datetime import timedelta

import pytest
from juju import JujuClient

from .scheduler.states import State

_WAIT_FOR_IDLE_TIMEOUT = timedelta(minutes=15)


@pytest.mark.state(requires=State.NEIGHBOR_ONLY, provides=State.DEPLOYED)
def test_idempotent_redeploy(
    juju_client: JujuClient,
    model: str,
    target_application: str,
    bundles: list[str],
) -> None:
    """Redeploy all bundles and assert that the target application is present."""
    for bundle in bundles:
        juju_client.deploy_bundle_file(bundle, model=model)
    juju_client.idle_for_period(model=model, timeout=_WAIT_FOR_IDLE_TIMEOUT)
    assert juju_client.application_exists(
        target_application, model=model
    ), f"Application '{target_application}' was not found after redeploy"
