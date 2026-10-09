# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import datetime, timedelta, timezone
from typing import cast

import pytest
from kubernetes.client import (  # type: ignore[import-untyped]
    ApiException,
    V1DeleteOptions,
    V1ObjectMeta,
    V1Pod,
    V1PodList,
    V1PodStatus,
)
from kubernetes_client import KubernetesBackend, KubernetesClient, KubernetesExtension


def pod(uid: str, phase: str = "Running", *, name: str = "target-1", unit: str = "target/1") -> V1Pod:
    return V1Pod(
        metadata=V1ObjectMeta(name=name, uid=uid, annotations={"unit.juju.is/id": unit}),
        status=V1PodStatus(phase=phase),
    )


class Clock:
    now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr("kubernetes_client.client.monotonic", clock.monotonic)
    monkeypatch.setattr("kubernetes_client.client.sleep", clock.sleep)
    return clock


class PodAPI:
    def __init__(self, responses: list[list[V1Pod] | Exception], clock: Clock) -> None:
        self.responses = responses
        self.clock = clock
        self.body: V1DeleteOptions | None = None
        self.reads = 0
        self.delete_error: ApiException | None = None
        self.delete_duration = 0.0
        self.list_duration = 0.0
        self.timeouts: list[tuple[float, float]] = []

    def delete_namespaced_pod(
        self, name: str, namespace: str, body: V1DeleteOptions, _request_timeout: tuple[float, float]
    ) -> None:
        assert (name, namespace) == ("target-1", "model")
        self.body = body
        self.timeouts.append(_request_timeout)
        self.clock.sleep(self.delete_duration)
        if self.delete_error is not None:
            raise self.delete_error

    def list_namespaced_pod(
        self, namespace: str, label_selector: str, _request_timeout: tuple[float, float]
    ) -> V1PodList:
        assert namespace == "model"
        assert label_selector == "app.kubernetes.io/name=target"
        self.timeouts.append(_request_timeout)
        self.reads += 1
        self.clock.sleep(self.list_duration)
        result = self.responses.pop(0) if self.responses else []
        if isinstance(result, Exception):
            raise result
        return V1PodList(items=result)


class Backend(KubernetesBackend):
    def __init__(self, api: PodAPI) -> None:
        self.core_v1_api = api


class Hook(KubernetesExtension):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def post_delete_pod(self, namespace: str, pod_name: str) -> None:
        self.calls.append((namespace, pod_name))


def restart(api: PodAPI, hook: Hook) -> None:
    KubernetesClient(Backend(api), extensions=[hook]).restart_pod(
        "model",
        "target-1",
        "old",
        timedelta(seconds=10),
        application="target",
        unit="target/1",
        existing_uids={"old", "sibling"},
    )


@pytest.mark.parametrize("new_name", ["target-1", "target-replicaset-new"])
def test_waits_for_replacement_before_hook(clock: Clock, new_name: str) -> None:
    api = PodAPI(
        [
            [pod("old")],
            ApiException(status=404),
            [],
            [pod("new", "Pending", name=new_name)],
            [pod("new", name=new_name)],
        ],
        clock,
    )
    hook = Hook()
    restart(api, hook)
    assert cast(V1DeleteOptions, api.body).preconditions.uid == "old"
    assert api.reads == 5
    assert hook.calls == [("model", new_name)]


@pytest.mark.parametrize("candidate", ["sibling", "wrong-unit", "unannotated", "deleting", "no-uid"])
def test_ignores_pods_that_are_not_a_live_replacement(clock: Clock, candidate: str) -> None:
    wrong = pod("new")
    if candidate == "sibling":
        wrong.metadata.uid = "sibling"  # Even a known Pod with the target annotation is not new.
    elif candidate == "wrong-unit":
        wrong.metadata.annotations = {"unit.juju.is/id": "target/2"}
    elif candidate == "unannotated":
        wrong.metadata.annotations = None
    elif candidate == "deleting":
        wrong.metadata.deletion_timestamp = datetime.now(timezone.utc)
    else:
        wrong.metadata.uid = None
    api = PodAPI([[wrong], [pod("replacement", name="replacement")]], clock)
    hook = Hook()
    restart(api, hook)
    assert api.reads == 2
    assert hook.calls == [("model", "replacement")]


@pytest.mark.parametrize("becomes_running", [True, False])
def test_replacement_without_status_waits_for_running(clock: Clock, becomes_running: bool) -> None:
    # GIVEN a replacement Pod whose status has not been populated yet
    replacement = pod("new")
    replacement.status = None
    api = PodAPI([[replacement]] * 10, clock)
    hook = Hook()
    if becomes_running:
        api.responses = [[replacement], [pod("new")]]

    # WHEN waiting for replacement, THEN missing status does not complete the restart
    if becomes_running:
        restart(api, hook)
        assert api.reads == 2
        assert clock.now > 0
        assert hook.calls == [("model", "target-1")]
    else:
        with pytest.raises(TimeoutError, match="was not replaced"):
            restart(api, hook)
        assert clock.now == 10
        assert hook.calls == []


def test_ambiguous_replacements_are_not_accepted(clock: Clock) -> None:
    api = PodAPI([[pod("new-1"), pod("new-2")]], clock)
    hook = Hook()
    with pytest.raises(TimeoutError):
        restart(api, hook)
    assert hook.calls == []


def test_api_failure_is_not_treated_as_replacement(clock: Clock) -> None:
    api = PodAPI([ApiException(status=403)], clock)
    hook = Hook()
    with pytest.raises(ApiException):
        restart(api, hook)
    assert hook.calls == []


def test_uid_conflict_does_not_wait_or_run_hook(clock: Clock) -> None:
    api = PodAPI([], clock)
    api.delete_error = ApiException(status=409)
    hook = Hook()
    with pytest.raises(ApiException) as error:
        restart(api, hook)
    assert error.value.status == 409
    assert cast(V1DeleteOptions, api.body).preconditions.uid == "old"
    assert api.reads == 0
    assert hook.calls == []


def test_delete_and_polling_share_timeout_budget(clock: Clock) -> None:
    api = PodAPI([], clock)
    api.delete_duration = 7
    hook = Hook()
    with pytest.raises(TimeoutError, match="was not replaced"):
        restart(api, hook)
    assert clock.now == 10
    assert api.timeouts == [(5, 5), (1.5, 1.5), (1, 1), (0.5, 0.5)]
    assert hook.calls == []


def test_delete_exhausting_budget_does_not_poll(clock: Clock) -> None:
    api = PodAPI([], clock)
    api.delete_duration = 10
    hook = Hook()
    with pytest.raises(TimeoutError):
        restart(api, hook)
    assert api.reads == 0
    assert hook.calls == []


def test_replacement_returned_after_deadline_does_not_run_hook(clock: Clock) -> None:
    api = PodAPI([[pod("new")]], clock)
    api.list_duration = 11
    hook = Hook()
    with pytest.raises(TimeoutError):
        restart(api, hook)
    assert hook.calls == []
