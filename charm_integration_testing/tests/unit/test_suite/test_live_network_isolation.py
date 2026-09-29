# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from chaos_client import ChaosCleanupError, MetaChaosClient
from chaos_client.adapters import NetworkIsolationClient
from juju import JujuClient, JujuModelHandle, JujuWaitState, JujuWaitTimeoutError
from kubernetes.client import V1DeleteOptions, V1NetworkPolicy  # type: ignore[import-untyped]
from kubernetes_client import KubernetesBackend, KubernetesClient
from test_suite.test_live_network_isolation import test_live_network_isolation as run_isolation

from ..chaos_client.shared import FakeNetworkingV1Api

MODEL = JujuModelHandle(controller="controller", model="model")


class NetworkApi(FakeNetworkingV1Api):
    def __init__(self, events: list[str], failure: str | None) -> None:
        super().__init__()
        self.events = events
        self.failure = failure

    def create_namespaced_network_policy(self, namespace: str, body: V1NetworkPolicy) -> None:
        self.events.append("isolate")
        super().create_namespaced_network_policy(namespace=namespace, body=body)
        if self.failure == "create":
            raise TimeoutError("create response lost")

    def delete_namespaced_network_policy(self, name: str, namespace: str, body: V1DeleteOptions | None = None) -> None:
        self.events.append("cleanup")
        super().delete_namespaced_network_policy(name=name, namespace=namespace, body=body)


class Backend(KubernetesBackend):
    def __init__(self, api: NetworkApi) -> None:
        self.networking_v1_api = api


class JujuSpy(JujuClient):
    def __init__(self, events: list[str], failure: str | None) -> None:
        self.events = events
        self.failure = failure
        self.error = JujuWaitTimeoutError(JujuWaitState(message=failure or "waiting"))
        self.idle_calls = 0

    def idle_for_period(
        self,
        model: JujuModelHandle,
        timeout: timedelta | None = None,
        count: int = 10,
        strict_timeout: bool = False,
    ) -> None:
        assert model == MODEL
        assert timeout == timedelta(minutes=15)
        assert strict_timeout
        phase = "baseline" if self.idle_calls == 0 else "recovery"
        self.idle_calls += 1
        self.events.append(phase)
        if self.failure == phase:
            raise self.error

    def unhealthy_for_period(
        self,
        application: str,
        model: JujuModelHandle,
        timeout: timedelta | None = None,
        count: int = 10,
        strict_timeout: bool = False,
    ) -> None:
        assert application == "target"
        assert model == MODEL
        assert timeout == timedelta(minutes=10)
        assert strict_timeout
        self.events.append("unhealthy")
        if self.failure in {"timeout", "agent-disconnected"}:
            raise self.error

    def validate_model(self, model: JujuModelHandle, level: str = "simple") -> None:
        assert model == MODEL
        assert level == "simple"
        self.events.append("validate")
        if self.failure == "validation":
            raise RuntimeError("validation failed")


@pytest.mark.parametrize(
    "failure", [None, "baseline", "create", "timeout", "agent-disconnected", "cleanup", "recovery", "validation"]
)
def test_isolation_lifecycle(failure: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    # GIVEN a real MetaChaosClient and network adapter backed by an in-memory API
    events: list[str] = []
    probe = MagicMock()
    monkeypatch.setattr("test_suite.test_live_network_isolation.NetworkIsolationProbe", lambda *args: probe)

    def after_removal(succeeded: bool) -> None:
        assert events[-1] == "cleanup"
        assert succeeded == (failure != "cleanup")

    probe.after_removal.side_effect = after_removal
    api = NetworkApi(events, failure)
    backend = Backend(api)
    chaos = MetaChaosClient([NetworkIsolationClient(backend)])
    juju = JujuSpy(events, failure)
    if failure == "cleanup":
        api.raise_on_delete = RuntimeError("delete failed")

    # WHEN the integration test runs, THEN errors cannot turn into a successful validation
    if failure is None:
        run_isolation(juju, chaos, KubernetesClient(backend), MODEL, "target", {}, "cloud")
        assert events == ["baseline", "isolate", "unhealthy", "cleanup", "recovery", "validate"]
    else:
        error_type = (
            ChaosCleanupError if failure == "cleanup" else (RuntimeError if failure == "validation" else TimeoutError)
        )
        with pytest.raises(error_type):
            run_isolation(juju, chaos, KubernetesClient(backend), MODEL, "target", {}, "cloud")
        if failure in {"baseline", "create", "timeout", "agent-disconnected", "cleanup"}:
            assert "recovery" not in events
            assert "validate" not in events

    # THEN ambiguous creation is cleaned up; failed deletion remains retryable at teardown
    if failure == "cleanup":
        assert api.policies
        api.raise_on_delete = None
    else:
        assert not api.policies
    chaos.cleanup_all()
    assert not api.policies
    if failure == "baseline":
        assert not api.create_calls
        probe.prepare.assert_not_called()
    else:
        probe.cleanup.assert_called_once()
        probe.after_removal.assert_called_once()


def test_non_kubernetes_skips_before_mutation() -> None:
    # GIVEN a machine environment, WHEN called, THEN skip without any health checks or policies
    events: list[str] = []
    api = NetworkApi(events, None)
    chaos = MetaChaosClient([NetworkIsolationClient(Backend(api))])
    with pytest.raises(pytest.skip.Exception, match="requires Kubernetes"):
        run_isolation(JujuSpy(events, None), chaos, None, MODEL, "target", {}, "cloud")
    assert events == []
    assert not api.policies
