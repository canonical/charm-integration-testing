# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from kubernetes_client import KubernetesBackend


def workload_target(
    backend: KubernetesBackend,
    namespace: str,
    unit: str,
    *,
    request_timeout: float | tuple[float, float],
    target_container: str | None = None,
) -> tuple[str, str]:
    """Resolve one live unit Pod and its workload container for targeted stress."""
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
    pod = matches[0]
    containers = [
        container.name
        for container in pod.spec.containers
        if any(env.name == "JUJU_CONTAINER_NAME" and env.value == container.name for env in container.env or [])
    ]
    if target_container is not None:
        containers = [name for name in containers if name == target_container]
    if len(containers) != 1:
        raise RuntimeError(f"Specify one workload container for {namespace}/{unit}; candidates: {containers}.")
    return str(pod.metadata.name), containers[0]
