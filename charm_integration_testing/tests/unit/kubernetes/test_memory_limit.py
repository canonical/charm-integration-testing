# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from copy import deepcopy
from typing import Any
from unittest.mock import MagicMock

import pytest
from kubernetes_client import KubernetesClient
from kubernetes_client.backend import KubernetesExtension
from kubernetes_client.memory_limit import temporary_memory_limit


@pytest.mark.parametrize("saved", [{}, {"limits": {"memory": "2Gi", "cpu": "1"}, "requests": {"memory": "2Gi"}}])
@pytest.mark.parametrize("failure", [None, "body", "patch", "wait", "replacement", "concurrent", "cpu"])
def test_restores_memory_after_success_or_failure(saved: dict[str, Any], failure: str | None) -> None:
    # GIVEN a StatefulSet with optional pre-existing memory and CPU resources
    events: list[str] = []

    class Extension(KubernetesExtension):
        def post_restart_statefulset(self, namespace: str, statefulset_name: str) -> None:
            raise AssertionError("Memory resource patches must not invoke restart hooks")

    kubernetes = KubernetesClient(backend=MagicMock(), extensions=[Extension()])

    def wait(*args: Any, **kwargs: Any) -> None:
        events.append("wait")
        if failure == "wait" and events.count("wait") == 1:
            raise TimeoutError("rollout timed out")

    kubernetes.wait_for_statefulset_restart = MagicMock(side_effect=wait)
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
        with temporary_memory_limit(kubernetes, "model", "app", "uid", "workload", 60, "1Gi"):
            assert events == ["patch", "wait"]
            resources = value["spec"]["template"]["spec"]["containers"][0]["resources"]
            assert resources["limits"]["memory"] == "1Gi"
            assert resources["requests"]["memory"] == ("1Gi" if saved else "128Mi")
            if failure == "replacement":
                value["metadata"]["uid"] = "replacement"
            if failure == "concurrent":
                resources["limits"]["memory"] = "3Gi"
            if failure == "cpu":
                resources["limits"]["cpu"] = "2"
            if failure == "body":
                raise TimeoutError("no stress response")

    # WHEN execution finishes, or fails after the limit has been applied
    if failure in (None, "cpu"):
        exercise()
    else:
        error = RuntimeError if failure in ("replacement", "concurrent") else TimeoutError
        with pytest.raises(error):
            exercise()
    if failure in ("replacement", "concurrent"):
        assert patches == 1
        return
    if failure == "cpu":
        saved = deepcopy(saved)
        saved.setdefault("limits", {})["cpu"] = "2"
    # THEN the original resources are restored, including an absent memory limit/request
    assert value["spec"]["template"]["spec"]["containers"][0]["resources"] == saved
    assert patches == 2

    assert events[-2:] == ["patch", "wait"]


@pytest.mark.parametrize("limit", ["0", "-1Gi"])
def test_non_positive_memory_limit_does_not_access_cluster(limit: str) -> None:
    kubernetes = MagicMock()
    with pytest.raises(ValueError, match="Memory limit must be positive"):
        with temporary_memory_limit(kubernetes, "model", "app", "uid", "workload", 60, limit):
            pytest.fail("Invalid limit was accepted")
    assert kubernetes.mock_calls == []
