# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta

import pytest
from chaos_client import ChaosCleanupError, MetaChaosClient, ResourceConstraintsClient
from chaos_client.adapters import NetworkIsolationClient
from juju import JujuApplicationInfo, JujuClient, JujuModelHandle, JujuWaitState, JujuWaitTimeoutError
from kubernetes.client import V1DeleteOptions, V1NetworkPolicy  # type: ignore[import-untyped]
from kubernetes_client import KubernetesBackend, KubernetesClient
from test_suite.test_live_network_isolation import test_live_network_isolation as run_isolation

from ..chaos_client.shared import FakeNetworkingV1Api
from ..extensions.shared import NullJujuBackend

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


class JujuBackendStub(NullJujuBackend):
    def list_applications(self, model: object) -> dict[str, JujuApplicationInfo]:
        return {}


class JujuSpy(JujuClient):
    def __init__(self, events: list[str], failure: str | None) -> None:
        self.events = events
        self.failure = failure
        self.error = JujuWaitTimeoutError(JujuWaitState(message=failure or "waiting"))
        self.idle_calls = 0

    def multi_model_idle_for_period(
        self,
        models: list[JujuModelHandle],
        timeout: timedelta | None = None,
        count: int = 10,
        strict_timeout: bool = False,
    ) -> None:
        assert models[0] == MODEL
        assert timeout == timedelta(minutes=15)
        assert strict_timeout
        phase = "baseline" if self.idle_calls == 0 else "recovery"
        self.idle_calls += 1
        self.events.append(phase)
        if self.failure == phase:
            raise self.error

    def validate_model(self, model: JujuModelHandle, level: str | None = "simple") -> None:
        assert model == MODEL
        assert level == "deep"
        self.events.append("validate")
        if self.failure == "validation":
            raise RuntimeError("validation failed")


@pytest.mark.parametrize("failure", [None, "baseline", "create", "observation", "cleanup", "recovery", "validation"])
def test_isolation_lifecycle(failure: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    # GIVEN a real MetaChaosClient and network adapter backed by an in-memory API
    events: list[str] = []

    def observe(seconds: float) -> None:
        assert seconds == 600
        assert api.policies
        events.append("observe")
        if failure == "observation":
            raise TimeoutError("observation failed")

    monkeypatch.setattr("test_suite.test_live_network_isolation.sleep", observe)
    api = NetworkApi(events, failure)
    backend = Backend(api)
    chaos = MetaChaosClient([NetworkIsolationClient(backend)], JujuBackendStub(), ResourceConstraintsClient())
    juju = JujuSpy(events, failure)
    if failure == "cleanup":
        api.raise_on_delete = RuntimeError("delete failed")

    # WHEN the integration test runs, THEN errors cannot turn into a successful validation
    if failure is None:
        run_isolation(
            juju, chaos, KubernetesClient(backend), MODEL, "target", None, timedelta(minutes=10), timedelta(minutes=15)
        )
        assert events == ["baseline", "isolate", "observe", "cleanup", "recovery", "validate"]
    else:
        error_type = (
            ChaosCleanupError if failure == "cleanup" else (RuntimeError if failure == "validation" else TimeoutError)
        )
        with pytest.raises(error_type):
            run_isolation(
                juju,
                chaos,
                KubernetesClient(backend),
                MODEL,
                "target",
                None,
                timedelta(minutes=10),
                timedelta(minutes=15),
            )
        if failure in {"baseline", "create", "observation", "cleanup"}:
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


def test_non_kubernetes_skips_before_mutation() -> None:
    # GIVEN a machine environment, WHEN called, THEN skip without any health checks or policies
    events: list[str] = []
    api = NetworkApi(events, None)
    chaos = MetaChaosClient([NetworkIsolationClient(Backend(api))], JujuBackendStub(), ResourceConstraintsClient())
    with pytest.raises(pytest.skip.Exception, match="requires Kubernetes"):
        run_isolation(
            JujuSpy(events, None), chaos, None, MODEL, "target", None, timedelta(minutes=10), timedelta(minutes=15)
        )
    assert events == []
    assert not api.policies


@pytest.mark.parametrize("neighbor", [None, MODEL, JujuModelHandle(controller="other", model="neighbor-model")])
def test_validates_all_models_after_recovery(neighbor: JujuModelHandle | None, monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock

    juju = MagicMock(spec=JujuClient)
    chaos = MagicMock(spec=MetaChaosClient)
    monkeypatch.setattr("test_suite.test_live_network_isolation.sleep", lambda seconds: None)
    run_isolation(juju, chaos, MagicMock(), MODEL, "target", neighbor, timedelta(seconds=1), timedelta(minutes=15))
    models = [MODEL] if neighbor in (None, MODEL) else [MODEL, neighbor]
    assert juju.multi_model_idle_for_period.call_count == 2
    juju.multi_model_idle_for_period.assert_called_with(
        models=models, timeout=timedelta(minutes=15), strict_timeout=True
    )
    assert [call.kwargs for call in juju.validate_model.call_args_list] == [
        {"model": model, "level": "deep"} for model in models
    ]
