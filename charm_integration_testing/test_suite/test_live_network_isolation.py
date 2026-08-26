# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta

import pytest
from chaos_client import ChaosClient
from juju import JujuClient

from .scheduler.states import State


@pytest.mark.state(requires=State.DEPLOYED)
def test_live_network_isolation(
    juju_client: JujuClient,
    _is_running_on_kubernetes: None,
    chaos_client: ChaosClient | None,
    model: str,
    target_application: str,
) -> None:
    if chaos_client is None:
        pytest.fail("ChaosClient was not instantiated correctly. Is KUBECONFIG set?")

    unit = f"{target_application}/0"

    try:
        chaos_client.isolate_network(model=model, unit=unit)
        # Debounced against update-status blips; fails immediately if the Juju agent disconnects.
        juju_client.unhealthy_for_period(target_application, model=model, timeout=timedelta(minutes=10))
    finally:
        chaos_client.remove_network_isolation(model=model, unit=unit)

    # Wait for self-recovery
    juju_client.idle_for_period(model=model, timeout=timedelta(minutes=15))
    juju_client.validate_model(model=model, level="simple")
