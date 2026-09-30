# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import datetime, timedelta, timezone
from time import monotonic, sleep
from typing import Callable

import pytest
from chaos_client import MetaChaosClient, ResourceConstraintsClient
from juju import JujuClient, JujuModelHandle
from kubernetes.utils.quantity import parse_quantity  # type: ignore[import-untyped]
from kubernetes_client import KubernetesClient
from kubernetes_client.memory_limit import temporary_memory_limit

from bundle_builder_x.charm import CharmChannel

from .fixtures.chaos_tools import ChaosTool, available_chaos_tools
from .scheduler.states import State


def observe_memory_stress(
    chaos: MetaChaosClient,
    model: JujuModelHandle,
    unit: str,
    seconds: float,
    *,
    oom_detected: Callable[[], bool],
) -> None:
    """Observe until a new target OOM or the configured window ends."""
    deadline = monotonic() + seconds
    while True:
        exhausted = oom_detected()
        chaos.check_stress(model, unit, allow_completed=exhausted)
        if exhausted:
            return
        remaining = deadline - monotonic()
        if remaining <= 0:
            return
        sleep(min(10, remaining))


@pytest.fixture
def memory_limit() -> str:
    """Temporary workload container limit, independent of chaos parameters."""
    return "1Gi"


@pytest.fixture
def memory_recovery_timeout() -> timedelta:
    """Maximum wait for all bundle units to recover after stress cleanup."""
    return timedelta(minutes=15)


@pytest.mark.state(requires=State.DEPLOYED, provides=State.DEPLOYED)
def test_live_memory_stress_total(
    juju_client: JujuClient,
    chaos_tool_for_model: Callable[[JujuModelHandle], MetaChaosClient],
    target_model_ref: JujuModelHandle,
    target_application: str,
    memory_limit: str,
    resource_constraints_client: ResourceConstraintsClient,
    memory_recovery_timeout: timedelta,
    kubernetes_client: KubernetesClient | None,
    neighbor_model_ref: JujuModelHandle | None,
) -> None:
    backend = juju_client.backend
    kubernetes = kubernetes_client
    if kubernetes is None:
        pytest.skip("Total memory stress requires Kubernetes.")
    tools = available_chaos_tools(kubernetes.backend)
    if not tools:
        pytest.skip("Total memory stress requires Litmus or Chaos Mesh.")
    if ChaosTool.LITMUS not in tools and not kubernetes.backend.crd_exists("stresschaos.chaos-mesh.org"):
        pytest.skip("Chaos Mesh memory stress requires the StressChaos CRD.")

    info = backend.list_applications(target_model_ref)[target_application]
    if info.channel is None or info.base is None:
        pytest.fail("Deployed charm channel and Ubuntu base are required to resolve memory settings.")
    settings = resource_constraints_client.get_charm_resource_constraints(
        info.charm, CharmChannel.model_validate(str(info.channel)), info.base
    )
    workers = settings.memory_exhaustion_workers or 1
    size_mb = settings.memory_exhaustion_size_mb or 2048
    memory_stress_duration = timedelta(seconds=settings.memory_exhaustion_duration_seconds or 600)
    if memory_recovery_timeout.total_seconds() <= 0 or parse_quantity(memory_limit) <= 0:
        raise ValueError("Memory limit and recovery timeout must be positive.")
    # Require the size parameter to cover the limit, treating MB conservatively as decimal.
    if size_mb * 1_000_000 < parse_quantity(memory_limit):
        raise ValueError("Total memory stress size must be at least the container memory limit.")
    juju_client.logger.info(
        "Memory stress settings: limit=%s, workers=%s, size_mb=%s, duration=%s",
        memory_limit,
        workers,
        size_mb,
        memory_stress_duration,
    )
    models_to_validate = list(
        dict.fromkeys(model for model in (target_model_ref, neighbor_model_ref) if model is not None)
    )
    juju_client.multi_model_idle_for_period(
        models=models_to_validate, timeout=memory_recovery_timeout, strict_timeout=True
    )

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
        pytest.fail("Memory limit setup requires a StatefulSet-owned Juju Pod.")
    containers = [
        item.name
        for item in pod.spec.containers
        if any(env.name == "JUJU_CONTAINER_NAME" and env.value == item.name for env in item.env or [])
    ]
    if len(containers) != 1:
        pytest.fail(f"Expected one workload container, found {containers}.")
    owner = owners[0]
    chaos = chaos_tool_for_model(target_model_ref)
    with temporary_memory_limit(
        kubernetes,
        namespace,
        owner.name,
        owner.uid,
        containers[0],
        int(memory_recovery_timeout.total_seconds()),
        memory_limit,
    ):
        # Ignore rollout transitions: establish a healthy baseline before stress.
        juju_client.multi_model_idle_for_period(
            models=models_to_validate, timeout=memory_recovery_timeout, strict_timeout=True
        )
        current_pods = kubernetes.get_charm_pods(application_name=target_application, model=namespace)
        targets = [
            item
            for item in current_pods
            if (item.metadata.annotations or {}).get("unit.juju.is/id") == unit
            and item.metadata.deletion_timestamp is None
            and (item.status is None or item.status.phase not in {"Succeeded", "Failed"})
        ]
        if len(targets) != 1:
            pytest.fail(f"Expected one Pod for {unit} after memory limit rollout.")
        workload = next(item for item in targets[0].spec.containers if item.name == containers[0])
        limit = (workload.resources.limits or {}).get("memory") if workload.resources else None
        if limit is None or parse_quantity(limit) != parse_quantity(memory_limit):
            pytest.fail(f"Memory limit was not applied to {unit}/{containers[0]}.")
        baseline = targets[0]
        baseline_statuses = baseline.status.container_statuses or [] if baseline.status else []
        baseline_status = next((state for state in baseline_statuses if state.name == containers[0]), None)
        if baseline_status is None or not baseline.metadata.uid:
            pytest.fail("Workload container status and Pod UID are required before memory stress.")
        baseline_uid = baseline.metadata.uid
        baseline_restarts = baseline_status.restart_count

        def oom_detected() -> bool:
            observed = kubernetes.get_charm_pods(application_name=target_application, model=namespace)
            for item in observed:
                if item.metadata.uid != baseline_uid or item.status is None:
                    continue
                for state in item.status.container_statuses or []:
                    terminated = state.last_state.terminated if state.last_state else None
                    if (
                        state.name == containers[0]
                        and state.restart_count > baseline_restarts
                        and terminated is not None
                        and terminated.reason == "OOMKilled"
                        and terminated.exit_code == 137
                        and terminated.finished_at is not None
                        and terminated.finished_at >= injection_confirmed_at
                    ):
                        juju_client.logger.info(
                            "Memory exhaustion confirmed: unit=%s container=%s pod_uid=%s; verifying recovery.",
                            unit,
                            containers[0],
                            baseline_uid,
                        )
                        return True
            return False

        observation_error: Exception | None = None
        try:
            chaos.stress_memory(
                target_model_ref,
                unit,
                workers=workers,
                size_mb=size_mb,
                duration=memory_stress_duration + timedelta(minutes=2),
            )
            injection_confirmed_at = datetime.now(timezone.utc)
            # A new target OOM confirms exhaustion; otherwise observe for the full window.
            observe_memory_stress(
                chaos=chaos,
                model=target_model_ref,
                unit=unit,
                seconds=memory_stress_duration.total_seconds(),
                oom_detected=oom_detected,
            )
        except Exception as error:
            observation_error = error
            raise
        finally:
            try:
                chaos.cleanup_all()
            except Exception as cleanup_error:
                if observation_error is not None:
                    raise RuntimeError(
                        f"Memory stress failed: {observation_error!r}; cleanup also failed: {cleanup_error!r}"
                    ) from cleanup_error
                raise
        # Verify recovery while the memory limit remains in place, before its restore
        # triggers a rollout that could otherwise conceal a failure to self-recover.
        juju_client.multi_model_idle_for_period(
            models=models_to_validate, timeout=memory_recovery_timeout, strict_timeout=True
        )
        for model_ref in models_to_validate:
            juju_client.validate_model(model=model_ref, level="deep")
    juju_client.multi_model_idle_for_period(
        models=models_to_validate, timeout=memory_recovery_timeout, strict_timeout=True
    )
