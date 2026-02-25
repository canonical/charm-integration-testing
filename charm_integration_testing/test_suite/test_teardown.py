# Copyright 2025-2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Remove the target application from the model.

This is a *transition test*: it moves the environment from ``deployed`` to
``neighbor_only``.  The scheduler will inject it automatically when any
subsequent test requires the ``neighbor_only`` state.
"""

from datetime import timedelta

import pytest
from juju import JujuClient

from .scheduler.states import State

_WAIT_FOR_REMOVAL_TIMEOUT = timedelta(minutes=15)


@pytest.mark.state(requires=State.DEPLOYED, provides=State.NEIGHBOR_ONLY)
def test_teardown(juju_client: JujuClient, model: str, target_application: str) -> None:
    """Remove the target application from the model and wait until it is gone."""
    juju_client.remove_applications(target_application, model=model)
    juju_client.wait_for_removal(target_application, model=model, timeout=_WAIT_FOR_REMOVAL_TIMEOUT)
