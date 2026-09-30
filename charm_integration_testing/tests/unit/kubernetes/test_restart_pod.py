# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta
from typing import cast

import pytest
from kubernetes.client import (  # type: ignore[import-untyped]
    ApiException,
    V1DeleteOptions,
    V1ObjectMeta,
    V1Pod,
    V1PodStatus,
)
from kubernetes_client import KubernetesBackend, KubernetesClient, KubernetesExtension


def pod(uid: str, phase: str = "Running") -> V1Pod:
    return V1Pod(metadata=V1ObjectMeta(name="target-1", uid=uid), status=V1PodStatus(phase=phase))


class PodAPI:
    def __init__(self, responses: list[V1Pod | Exception]) -> None:
        self.responses = responses
        self.body: V1DeleteOptions | None = None
        self.reads = 0
        self.delete_error: ApiException | None = None

    def delete_namespaced_pod(self, name: str, namespace: str, body: V1DeleteOptions, _request_timeout: int) -> None:
        assert (name, namespace) == ("target-1", "model")
        self.body = body
        if self.delete_error is not None:
            raise self.delete_error

    def read_namespaced_pod(self, name: str, namespace: str, _request_timeout: int) -> V1Pod:
        assert (name, namespace) == ("target-1", "model")
        self.reads += 1
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class Backend(KubernetesBackend):
    def __init__(self, api: PodAPI) -> None:
        self.core_v1_api = api


class Hook(KubernetesExtension):
    def __init__(self, api: PodAPI) -> None:
        self.api = api
        self.calls = 0

    def post_delete_pod(self, namespace: str, pod_name: str) -> None:
        assert not self.api.responses
        self.calls += 1


def test_waits_for_exact_replacement_before_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    api = PodAPI([pod("old"), ApiException(status=404), pod("new", "Pending"), pod("new")])
    hook = Hook(api)
    client = KubernetesClient(Backend(api), extensions=[hook])
    monkeypatch.setattr("kubernetes_client.client.sleep", lambda _: None)
    client.restart_pod("model", "target-1", "old", timedelta(seconds=10))
    assert cast(V1DeleteOptions, api.body).preconditions.uid == "old"
    assert api.reads == 4
    assert hook.calls == 1


def test_api_failure_is_not_treated_as_replacement() -> None:
    api = PodAPI([ApiException(status=403)])
    hook = Hook(api)
    client = KubernetesClient(Backend(api), extensions=[hook])
    with pytest.raises(ApiException):
        client.restart_pod("model", "target-1", "old", timedelta(seconds=10))
    assert hook.calls == 0


def test_uid_conflict_does_not_wait_or_run_hook() -> None:
    api = PodAPI([])
    api.delete_error = ApiException(status=409)
    hook = Hook(api)
    client = KubernetesClient(Backend(api), extensions=[hook])

    with pytest.raises(ApiException) as error:
        client.restart_pod("model", "target-1", "old", timedelta(seconds=10))

    assert error.value.status == 409
    assert cast(V1DeleteOptions, api.body).preconditions.uid == "old"
    assert api.reads == 0
    assert hook.calls == 0
