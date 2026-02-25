# Copyright 2025-2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Deploy the bundle under test.

This is a *transition test*: it moves the environment from ``empty_model``
to ``deployed``.  The scheduler will inject it automatically when any
subsequent test requires the ``deployed`` state.
"""

from datetime import timedelta

import pytest
from juju import JujuClient

from .scheduler.states import State

_WAIT_AFTER_EACH_BUNDLE = timedelta(seconds=10)
_WAIT_FOR_IDLE_TIMEOUT = timedelta(minutes=15)


@pytest.mark.state(requires=State.EMPTY_MODEL, provides=State.DEPLOYED)
def test_deploy(
    juju_client: JujuClient,
    model: str,
    bundles: list[str],
) -> None:
    """Deploy all configured bundles and wait for the model to become idle."""
    for bundle in bundles:
        juju_client.deploy_bundle_file(bundle, model=model, wait_after_deploy=_WAIT_AFTER_EACH_BUNDLE)
    juju_client.idle_for_period(model=model, timeout=_WAIT_FOR_IDLE_TIMEOUT)
