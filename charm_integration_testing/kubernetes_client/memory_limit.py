# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from contextlib import contextmanager
from copy import deepcopy
from typing import Any, Iterator

from kubernetes.utils.quantity import parse_quantity  # type: ignore[import-untyped]

from .client import KubernetesClient


@contextmanager
def temporary_memory_limit(
    kubernetes: KubernetesClient, namespace: str, name: str, uid: str, container: str, timeout: int, limit: str
) -> Iterator[None]:
    """Limit one StatefulSet workload container to a memory limit, then restore its memory resources."""
    if parse_quantity(limit) <= 0:
        raise ValueError("Memory limit must be positive.")
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
        raise RuntimeError("Memory stress requires a StatefulSet with an unpartitioned RollingUpdate strategy.")
    index, saved = resources(original)
    limited = deepcopy(saved)
    limited.setdefault("limits", {})["memory"] = limit
    request = (saved.get("requests") or {}).get("memory", "128Mi")
    limited.setdefault("requests", {})["memory"] = (
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
            actual = (restored.get(field) or {}).get("memory")
            previous = (saved.get(field) or {}).get("memory")
            expected = limited[field]["memory"]
            if not any(
                actual == candidate
                if actual is None or candidate is None
                else parse_quantity(actual) == parse_quantity(candidate)
                for candidate in (previous, expected)
            ):
                raise RuntimeError(f"Memory {field} changed concurrently for {namespace}/{name}/{container}.")
            if previous is None:
                restored.setdefault(field, {}).pop("memory", None)
                if not restored[field]:
                    restored.pop(field)
            else:
                restored.setdefault(field, {})["memory"] = previous
        patch(current, index, restored)
        kubernetes.wait_for_statefulset_restart(namespace, name, timeout_seconds=timeout)
