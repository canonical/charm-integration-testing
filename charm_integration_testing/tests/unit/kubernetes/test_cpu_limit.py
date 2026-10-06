# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from copy import deepcopy
from typing import Any
from unittest.mock import MagicMock

import pytest
from kubernetes_client import KubernetesClient
from kubernetes_client.backend import KubernetesExtension
from kubernetes_client.cpu_limit import temporary_cpu_limit


@pytest.mark.parametrize("saved", [{}, {"limits": {"cpu": "2", "memory": "1Gi"}, "requests": {"cpu": "1500m"}}])
@pytest.mark.parametrize("failure", [None, "body", "patch", "replacement", "concurrent", "memory"])
def test_restores_cpu_after_success_or_failure(saved: dict[str, Any], failure: str | None) -> None:
    # GIVEN a StatefulSet with optional pre-existing CPU and memory resources
    events: list[str] = []

    class Extension(KubernetesExtension):
        def post_restart_statefulset(self, namespace: str, statefulset_name: str) -> None:
            raise AssertionError("CPU resource patches must not invoke restart hooks")

    kubernetes = KubernetesClient(backend=MagicMock(), extensions=[Extension()])
    kubernetes.wait_for_statefulset_restart = MagicMock(side_effect=lambda *a, **kw: events.append("wait"))
    value: dict[str, Any] = {
        "metadata": {"uid": "uid", "resourceVersion": "1"},
        "spec": {"template": {"spec": {"containers": [{"name": "workload", "resources": deepcopy(saved)}]}}},
    }
    api = kubernetes.backend.apps_v1_api
    api.read_namespaced_stateful_set.side_effect = lambda **_: deepcopy(value)
    kubernetes.backend.api_client.sanitize_for_serialization.side_effect = lambda item: item
    patches = 0

    def patch(*, body: list[dict[str, Any]], **kwargs: Any) -> None:
        nonlocal patches
        assert body[0]["value"] == "uid"
        assert body[1]["value"] == value["metadata"]["resourceVersion"]
        value["spec"]["template"]["spec"]["containers"][0]["resources"] = deepcopy(body[2]["value"])
        events.append("patch")
        patches += 1
        value["metadata"]["resourceVersion"] = str(patches + 1)
        if failure == "patch" and patches == 1:
            raise TimeoutError("response lost")

    api.patch_namespaced_stateful_set.side_effect = patch

    def exercise() -> None:
        with temporary_cpu_limit(kubernetes, "model", "app", "uid", "workload", 60):
            assert events == ["patch", "wait"]
            resources = value["spec"]["template"]["spec"]["containers"][0]["resources"]
            assert resources["limits"]["cpu"] == "1"
            if failure == "replacement":
                value["metadata"]["uid"] = "replacement"
            if failure == "concurrent":
                resources["limits"]["cpu"] = "3"
            if failure == "memory":
                resources["limits"]["memory"] = "2Gi"
            if failure == "body":
                raise TimeoutError("no stress response")

    # WHEN execution finishes, or fails after the limit has been applied
    if failure in (None, "memory"):
        exercise()
    else:
        error = RuntimeError if failure in ("replacement", "concurrent") else TimeoutError
        with pytest.raises(error):
            exercise()
    if failure in ("replacement", "concurrent"):
        assert patches == 1
        return
    if failure == "memory":
        saved = deepcopy(saved)
        saved.setdefault("limits", {})["memory"] = "2Gi"
    # THEN the original resources are restored, including an absent CPU limit/request
    assert value["spec"]["template"]["spec"]["containers"][0]["resources"] == saved
    assert patches == 2

    assert events[-2:] == ["patch", "wait"]
