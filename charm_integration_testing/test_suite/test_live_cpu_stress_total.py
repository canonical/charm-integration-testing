# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta
from time import sleep
from typing import Callable

import pytest
from chaos_client import MetaChaosClient
from juju import JujuClient, JujuModelHandle
from kubernetes_client import KubernetesClient

from .fixtures.chaos_tools import ChaosTool, available_chaos_tools
from .scheduler.states import State


@pytest.fixture
def cpu_stress_duration() -> timedelta:
    """Time to keep CPU stress applied after injection is confirmed."""
    return timedelta(minutes=10)


@pytest.fixture
def cpu_recovery_timeout() -> timedelta:
    """Maximum wait for all bundle units to recover after stress cleanup."""
    return timedelta(minutes=15)


@pytest.mark.state(requires=State.DEPLOYED, provides=State.DEPLOYED)
def test_live_cpu_stress_total(
    juju_client: JujuClient,
    target_model_ref: JujuModelHandle,
    target_application: str,
    cpu_stress_duration: timedelta,
    cpu_recovery_timeout: timedelta,
    kubernetes_client: KubernetesClient | None,
    neighbor_model_ref: JujuModelHandle | None,
    neighbor_application: str,
    chaos_tool_for_model: Callable[[JujuModelHandle], MetaChaosClient],
) -> None:
    """Apply sustained CPU load without changing charm-managed resource settings."""
    kubernetes = kubernetes_client
    if kubernetes is None:
        pytest.skip("CPU stress requires Kubernetes.")
    tools = available_chaos_tools(kubernetes.backend)
    if not tools:
        pytest.skip("CPU stress requires Litmus or Chaos Mesh.")
    if ChaosTool.LITMUS not in tools and not kubernetes.backend.crd_exists("stresschaos.chaos-mesh.org"):
        pytest.skip("Chaos Mesh CPU stress requires the StressChaos CRD.")

    if cpu_stress_duration.total_seconds() <= 0 or cpu_recovery_timeout.total_seconds() <= 0:
        raise ValueError("CPU stress duration and recovery timeout must be positive.")
    models = list(dict.fromkeys([target_model_ref, *([neighbor_model_ref] if neighbor_model_ref else [])]))
    juju_client.multi_model_idle_for_period(models=models, timeout=cpu_recovery_timeout, strict_timeout=True)

    namespace = target_model_ref.model
    pods = kubernetes.get_charm_pods(application_name=target_application, model=namespace)
    pods = [
        pod
        for pod in pods
        if pod.metadata.deletion_timestamp is None
        and (pod.status is None or pod.status.phase not in {"Succeeded", "Failed"})
    ]
    if not pods:
        pytest.fail(f"No live Pods found for {namespace}/{target_application}.")
    pod = sorted(pods, key=lambda item: item.metadata.name)[0]
    unit = (pod.metadata.annotations or {}).get("unit.juju.is/id")
    if not unit or unit.split("/")[0] != target_application:
        pytest.fail("Target Pod has no matching Juju unit annotation.")
    containers = [
        item.name
        for item in pod.spec.containers
        if any(env.name == "JUJU_CONTAINER_NAME" and env.value == item.name for env in item.env or [])
    ]
    if not containers:
        pytest.fail(f"No workload containers found for {namespace}/{unit}.")
    chaos = chaos_tool_for_model(target_model_ref)
    try:
        chaos.stress_cpu(target_model_ref, unit, workers=4, duration=cpu_stress_duration + timedelta(minutes=2))
        # Surviving stress without a status change is valid. Keep the fault active
        # for the observation period instead of waiting for an unhealthy status.
        sleep(cpu_stress_duration.total_seconds())
        chaos.check_stress(target_model_ref, unit)
    finally:
        chaos.cleanup_all()
    juju_client.multi_model_idle_for_period(models=models, timeout=cpu_recovery_timeout, strict_timeout=True)
    juju_client.validate_model(model=target_model_ref, level="deep", applications=[target_application])
    # Consumer-side validators exercise the target's provided interface (for
    # example, postgresql_client runs on data-integrator, not PostgreSQL).
    juju_client.validate_model(
        model=neighbor_model_ref or target_model_ref, level="deep", applications=[neighbor_application]
    )
