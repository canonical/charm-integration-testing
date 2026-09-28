# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta

import pytest
from chaos_client import MetaChaosClient
from juju import JujuClient, JujuModelHandle
from kubernetes_client import KubernetesClient

from .scheduler.states import State


@pytest.mark.state(requires=State.DEPLOYED)
def test_live_network_isolation(
    juju_client: JujuClient,
    require_chaos_tool: MetaChaosClient,
    kubernetes_client: KubernetesClient | None,
    target_model_ref: JujuModelHandle,
    target_application: str,
) -> None:
    if kubernetes_client is None:
        pytest.skip("Network isolation requires Kubernetes.")

    unit = f"{target_application}/0"

    # Establish a healthy baseline so an existing failure cannot satisfy the test.
    juju_client.idle_for_period(model=target_model_ref, timeout=timedelta(minutes=15), strict_timeout=True)
    try:
        require_chaos_tool.isolate_network(model=target_model_ref.model, unit=unit)
        # Debounced against update-status blips; agent disconnection and timeout fail the test.
        juju_client.unhealthy_for_period(
            target_application, model=target_model_ref, timeout=timedelta(minutes=10), strict_timeout=True
        )
    finally:
        require_chaos_tool.remove_network_isolation(model=target_model_ref.model, unit=unit)

    # Wait for self-recovery
    juju_client.idle_for_period(model=target_model_ref, timeout=timedelta(minutes=15), strict_timeout=True)
    juju_client.validate_model(model=target_model_ref, level="simple")
