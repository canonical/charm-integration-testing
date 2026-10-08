# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta
from time import sleep

import pytest
from chaos_client import MetaChaosClient
from juju import JujuClient, JujuModelHandle
from kubernetes_client import KubernetesClient

from .scheduler.states import State


@pytest.fixture
def network_isolation_duration() -> timedelta:
    """Time to retain the ingress policy after the API accepts it."""
    return timedelta(minutes=10)


@pytest.fixture
def network_recovery_timeout() -> timedelta:
    """Maximum wait for all bundle units to recover after policy removal."""
    return timedelta(minutes=15)


@pytest.mark.state(requires=State.DEPLOYED, provides=State.DEPLOYED)
def test_live_network_isolation(
    juju_client: JujuClient,
    require_chaos_tool: MetaChaosClient,
    kubernetes_client: KubernetesClient | None,
    target_model_ref: JujuModelHandle,
    target_application: str,
    neighbor_model_ref: JujuModelHandle | None,
    network_isolation_duration: timedelta,
    network_recovery_timeout: timedelta,
) -> None:
    if kubernetes_client is None:
        pytest.skip("Network isolation requires Kubernetes.")
    if network_isolation_duration.total_seconds() <= 0 or network_recovery_timeout.total_seconds() <= 0:
        raise ValueError("Network isolation duration and recovery timeout must be positive.")

    models = list(dict.fromkeys([target_model_ref, *([neighbor_model_ref] if neighbor_model_ref else [])]))
    juju_client.multi_model_idle_for_period(models=models, timeout=network_recovery_timeout, strict_timeout=True)
    # The policy selects all Pods of the application, not only unit zero.
    unit = f"{target_application}/0"
    try:
        require_chaos_tool.isolate_network(model=target_model_ref.model, unit=unit)
        # Remaining active/idle during isolation is valid. No restart or other
        # recovery intervention is performed after removing the ingress policy.
        sleep(network_isolation_duration.total_seconds())
    finally:
        require_chaos_tool.remove_network_isolation(model=target_model_ref.model, unit=unit)

    juju_client.multi_model_idle_for_period(models=models, timeout=network_recovery_timeout, strict_timeout=True)
    # Validate both provider and consumer applications, including cross-model
    # neighbors. Unsupported validators remain explicitly skipped.
    for model in models:
        juju_client.validate_model(model=model, level="deep")
