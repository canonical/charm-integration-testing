# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from contextlib import contextmanager
from copy import deepcopy
from typing import Any, Iterator, Literal

from kubernetes.utils.quantity import parse_quantity  # type: ignore[import-untyped]

from .client import KubernetesClient


@contextmanager
def temporary_resource_limit(
    kubernetes: KubernetesClient,
    namespace: str,
    name: str,
    uid: str,
    container: str,
    timeout: int,
    *,
    resource: Literal["cpu", "memory"],
    limit: str,
    default_request: str,
) -> Iterator[None]:
    """Apply and restore one resource limit with UID and concurrent-change guards.

    Preserve changes to unrelated resources. The caller supplies a validated limit
    and the default request to use when the container has no existing request.
    """
    label = "CPU" if resource == "cpu" else "Memory"
    api = kubernetes.backend.apps_v1_api

    def read() -> dict[str, Any]:
        obj = api.read_namespaced_stateful_set(name=name, namespace=namespace, _request_timeout=30)
        value: dict[str, Any] = kubernetes.backend.api_client.sanitize_for_serialization(obj)
        if value["metadata"]["uid"] != uid:
            raise RuntimeError(f"StatefulSet {namespace}/{name} was replaced.")
        return value

    def resources(value: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        for index, item in enumerate(value["spec"]["template"]["spec"]["containers"]):
            if item["name"] == container:
                return index, deepcopy(item.get("resources") or {})
        raise RuntimeError(f"Container {container} is missing from {namespace}/{name}.")

    def patch(value: dict[str, Any], index: int, updated: dict[str, Any]) -> None:
        kubernetes.patch_statefulset_template(
            statefulset_name=name,
            namespace=namespace,
            body=[
                {"op": "test", "path": "/metadata/uid", "value": uid},
                {"op": "test", "path": "/metadata/resourceVersion", "value": value["metadata"]["resourceVersion"]},
                {"op": "add", "path": f"/spec/template/spec/containers/{index}/resources", "value": updated},
            ],
        )

    original = read()
    strategy = original["spec"].get("updateStrategy") or {}
    if strategy.get("type", "RollingUpdate") != "RollingUpdate" or (strategy.get("rollingUpdate") or {}).get(
        "partition", 0
    ):
        raise RuntimeError(f"{label} stress requires a StatefulSet with an unpartitioned RollingUpdate strategy.")
    index, saved = resources(original)
    limited = deepcopy(saved)
    limited.setdefault("limits", {})[resource] = limit
    request = (saved.get("requests") or {}).get(resource, default_request)
    limited.setdefault("requests", {})[resource] = (
        request if parse_quantity(request) <= parse_quantity(limit) else limit
    )
    try:
        # Restore even when the API accepted the patch but its response was lost.
        patch(original, index, limited)
        kubernetes.wait_for_statefulset_restart(namespace, name, timeout_seconds=timeout)
        yield
    finally:
        current = read()
        index, restored = resources(current)
        for field in ("limits", "requests"):
            actual = (restored.get(field) or {}).get(resource)
            previous = (saved.get(field) or {}).get(resource)
            expected = limited[field][resource]
            if not any(
                actual == candidate
                if actual is None or candidate is None
                else parse_quantity(actual) == parse_quantity(candidate)
                for candidate in (previous, expected)
            ):
                raise RuntimeError(f"{label} {field} changed concurrently for {namespace}/{name}/{container}.")
            if previous is None:
                restored.setdefault(field, {}).pop(resource, None)
                if not restored[field]:
                    restored.pop(field)
            else:
                restored.setdefault(field, {})[resource] = previous
        patch(current, index, restored)
        kubernetes.wait_for_statefulset_restart(namespace, name, timeout_seconds=timeout)
