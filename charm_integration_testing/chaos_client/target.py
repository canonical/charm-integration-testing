# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from kubernetes import client as k8s  # type: ignore[import-untyped]
from kubernetes_client import KubernetesBackend


def _resolve_unit_pod(
    backend: KubernetesBackend,
    namespace: str,
    unit: str,
    *,
    request_timeout: float | tuple[float, float],
) -> k8s.V1Pod:
    """Resolve the single live Pod for a unit, without imposing any container requirement."""
    application = unit.split("/")[0]
    pods = backend.core_v1_api.list_namespaced_pod(
        namespace=namespace,
        label_selector=f"app.kubernetes.io/name={application}",
        _request_timeout=request_timeout,
    )
    matches = [
        pod
        for pod in pods.items
        if (pod.metadata.annotations or {}).get("unit.juju.is/id") == unit
        and pod.metadata.deletion_timestamp is None
        and (pod.status is None or pod.status.phase not in {"Succeeded", "Failed"})
    ]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one live Pod for {namespace}/{unit}, found {len(matches)}.")
    return matches[0]


def unit_pod_name(
    backend: KubernetesBackend,
    namespace: str,
    unit: str,
    *,
    request_timeout: float | tuple[float, float],
) -> str:
    """Resolve one live unit Pod's name, for experiments that target the whole Pod."""
    return str(_resolve_unit_pod(backend, namespace, unit, request_timeout=request_timeout).metadata.name)


def workload_targets(
    backend: KubernetesBackend,
    namespace: str,
    unit: str,
    *,
    request_timeout: float | tuple[float, float],
    target_container: str | None = None,
) -> tuple[str, list[str]]:
    """Resolve one live unit Pod and its Juju workload containers for stress."""
    pod = _resolve_unit_pod(backend, namespace, unit, request_timeout=request_timeout)
    containers = [
        container.name
        for container in pod.spec.containers
        if any(env.name == "JUJU_CONTAINER_NAME" and env.value == container.name for env in container.env or [])
    ]
    if target_container is not None:
        containers = [name for name in containers if name == target_container]
    if not containers:
        raise RuntimeError(f"No matching workload containers for {namespace}/{unit}.")
    return str(pod.metadata.name), containers
