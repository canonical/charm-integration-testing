# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta
from pathlib import Path

import pytest
from juju import JujuClient, JujuModelHandle
from kubernetes.utils.quantity import parse_quantity  # type: ignore[import-untyped]
from kubernetes_client import KubernetesClient
from kubernetes_client.cpu_limit import temporary_cpu_limit
from kubernetes_client.cpu_stress_diagnostics import log_cpu_stress_snapshot

from .fixtures.chaos_tools import ChaosTool, available_chaos_tools, chaos_client_for_model
from .scheduler.states import State
from .temporary_cpu_probe import CpuStressProbe


@pytest.fixture
def cpu_stress_timeout() -> timedelta:
    """Maximum wait for each stress response and recovery phase."""
    return timedelta(minutes=10)


@pytest.mark.state(requires=State.DEPLOYED, provides=State.DEPLOYED)
def test_live_cpu_stress_total(
    juju_client: JujuClient,
    target_model_ref: JujuModelHandle,
    target_application: str,
    cpu_stress_timeout: timedelta,
    kubernetes_client: KubernetesClient | None,
    cloud_kubeconfigs: dict[str, Path],
    target_cloud: str,
) -> None:
    backend = juju_client.backend
    kubernetes = kubernetes_client
    if kubernetes is None:
        pytest.skip("Total CPU stress requires Kubernetes.")
    tools = available_chaos_tools(kubernetes.backend)
    if not tools:
        pytest.skip("Total CPU stress requires Litmus or Chaos Mesh.")
    if ChaosTool.LITMUS not in tools and not kubernetes.backend.crd_exists("stresschaos.chaos-mesh.org"):
        pytest.skip("Chaos Mesh CPU stress requires the StressChaos CRD.")

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
    owners = [owner for owner in pod.metadata.owner_references or [] if owner.controller]
    if len(owners) != 1 or owners[0].kind != "StatefulSet":
        pytest.fail("CPU limit setup requires a StatefulSet-owned Juju Pod.")
    containers = [
        item.name
        for item in pod.spec.containers
        if any(env.name == "JUJU_CONTAINER_NAME" and env.value == item.name for env in item.env or [])
    ]
    if len(containers) != 1:
        pytest.fail(f"Expected one workload container, found {containers}.")
    owner = owners[0]
    chaos = chaos_client_for_model(backend, target_model_ref)
    with temporary_cpu_limit(
        kubernetes, namespace, owner.name, owner.uid, containers[0], int(cpu_stress_timeout.total_seconds())
    ):
        # Ignore rollout transitions: establish a healthy baseline before stress.
        juju_client.idle_for_period(model=target_model_ref, timeout=cpu_stress_timeout, strict_timeout=True)
        juju_client.wait_for_unit_health(target_model_ref, unit, True, cpu_stress_timeout)
        current_pods = kubernetes.get_charm_pods(application_name=target_application, model=namespace)
        targets = [
            item
            for item in current_pods
            if (item.metadata.annotations or {}).get("unit.juju.is/id") == unit
            and item.metadata.deletion_timestamp is None
            and (item.status is None or item.status.phase not in {"Succeeded", "Failed"})
        ]
        if len(targets) != 1:
            pytest.fail(f"Expected one Pod for {unit} after CPU limit rollout.")
        workload = next(item for item in targets[0].spec.containers if item.name == containers[0])
        limit = (workload.resources.limits or {}).get("cpu") if workload.resources else None
        if limit is None or parse_quantity(limit) != 1:
            pytest.fail(f"CPU limit was not applied to {unit}/{containers[0]}.")
        # TEMPORARY SQT-905: remove snapshots and helper after live diagnosis.
        probe = CpuStressProbe(
            cloud_kubeconfigs.get(target_cloud),
            target_model_ref.uri,
            namespace,
            targets[0].metadata.name,
            unit,
            containers[0],
        )
        probe.sample("before stress")
        log_cpu_stress_snapshot(kubernetes.backend, namespace, targets[0].metadata.name, "before stress")
        try:
            with probe.during_stress():
                chaos.stress_cpu(target_model_ref, unit, workers=4, duration=cpu_stress_timeout + timedelta(minutes=2))
                log_cpu_stress_snapshot(kubernetes.backend, namespace, targets[0].metadata.name, "after injection")
                juju_client.wait_for_unit_health(target_model_ref, unit, False, cpu_stress_timeout)
        finally:
            try:
                log_cpu_stress_snapshot(kubernetes.backend, namespace, targets[0].metadata.name, "before cleanup")
            finally:
                cleanup_succeeded = False
                try:
                    chaos.cleanup_all()
                    cleanup_succeeded = True
                finally:
                    probe.after_cleanup(cleanup_succeeded)
        # Recovery must occur before restoring the CPU limit, without a rollout or restart.
        juju_client.wait_for_unit_health(target_model_ref, unit, True, cpu_stress_timeout)
    juju_client.idle_for_period(model=target_model_ref, timeout=cpu_stress_timeout, strict_timeout=True)
    juju_client.validate_model(model=target_model_ref, level="simple")
