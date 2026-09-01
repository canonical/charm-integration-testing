# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta

import pytest
from chaos_client import ChaosClient
from juju import JujuClient, JujuModelHandle, JujuWaitTimeoutError, is_agent_disconnected

from .scheduler.states import State


@pytest.mark.state(requires=State.DEPLOYED)
def test_live_network_isolation(
    juju_client: JujuClient,
    chaos_client: ChaosClient | None,
    target_model_ref: JujuModelHandle,
    target_application: str,
) -> None:
    if chaos_client is None:
        pytest.skip("No ChaosClient for the target cloud (network isolation is Kubernetes-only).")

    unit = f"{target_application}/0"

    try:
        chaos_client.isolate_network(model=target_model_ref.model, unit=unit)
        try:
            # Debounced against update-status blips; fails immediately if the Juju agent disconnects.
            juju_client.unhealthy_for_period(target_application, model=target_model_ref, timeout=timedelta(minutes=10))
        except JujuWaitTimeoutError as exc:
            if is_agent_disconnected(exc.wait_state):
                raise
            # Still active at timeout; validators are the ground truth for workload health.
            juju_client.validate_model(model=target_model_ref, level="deep")
    finally:
        chaos_client.remove_network_isolation(model=target_model_ref.model, unit=unit)

    # Wait for self-recovery
    juju_client.idle_for_period(model=target_model_ref, timeout=timedelta(minutes=15))
    juju_client.validate_model(model=target_model_ref, level="simple")
