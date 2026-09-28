# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Iterator
from unittest.mock import MagicMock

import pytest
from juju import JujuClient, JujuModelHandle
from kubernetes import client  # type: ignore[import-untyped]
from test_suite import test_live_cpu_stress_total as module
from test_suite.fixtures.chaos_tools import ChaosTool

MODEL = JujuModelHandle(controller="controller", model="model")


@pytest.mark.parametrize("failure", [None, "stress", "status", "skip", "cleanup"])
def test_cleanup_and_recovery_order(monkeypatch: pytest.MonkeyPatch, failure: str | None) -> None:
    # GIVEN a single StatefulSet workload and recorded lifecycle operations
    juju = MagicMock(spec=JujuClient, backend=MagicMock())
    backend = juju.backend
    kubernetes = MagicMock()
    backend.get_kubernetes_client_for_model.side_effect = AssertionError("Use the fixture with extensions")
    pod = client.V1Pod(
        metadata=client.V1ObjectMeta(
            name="app-0",
            annotations={"unit.juju.is/id": "app/0"},
            owner_references=[
                client.V1OwnerReference(
                    api_version="apps/v1", kind="StatefulSet", name="app", uid="uid", controller=True
                )
            ],
        ),
        spec=client.V1PodSpec(
            containers=[
                client.V1Container(
                    name="workload",
                    resources=client.V1ResourceRequirements(limits={"cpu": "1"}),
                    env=[client.V1EnvVar(name="JUJU_CONTAINER_NAME", value="workload")],
                )
            ]
        ),
    )
    # Stale Pods must be ignored both before and after the limit rollout.
    completed = deepcopy(pod)
    completed.metadata.name = "aaa-completed"
    completed.status = client.V1PodStatus(phase="Succeeded")
    failed = deepcopy(completed)
    failed.metadata.name = "aaa-failed"
    failed.status.phase = "Failed"
    terminating = deepcopy(pod)
    terminating.metadata.name = "aaa-terminating"
    terminating.metadata.deletion_timestamp = datetime.now(timezone.utc)
    kubernetes.get_charm_pods.return_value = [completed, failed, terminating, pod]
    events: list[str] = []
    monkeypatch.setattr(module, "available_chaos_tools", lambda _: {ChaosTool.LITMUS})

    @contextmanager
    def limited(*args: object) -> Iterator[None]:
        events.append("limit")
        try:
            yield
        finally:
            events.append("restore")

    monkeypatch.setattr(module, "temporary_cpu_limit", limited)
    chaos = MagicMock()
    monkeypatch.setattr(module, "chaos_client_for_model", lambda *_: chaos)

    def stress(*args: object, **kwargs: object) -> None:
        events.append("stress")
        if failure == "stress":
            raise RuntimeError("stress failed")
        if failure == "skip":
            pytest.skip("tool disappeared")

    def cleanup() -> None:
        events.append("cleanup")
        if failure == "cleanup":
            raise RuntimeError("cleanup failed")

    def health(model: JujuModelHandle, unit: str, healthy: bool, timeout: timedelta) -> None:
        assert unit == "app/0"
        events.append("healthy" if healthy else "unhealthy")
        if failure == "status" and not healthy:
            raise TimeoutError("no response")

    chaos.stress_cpu.side_effect = stress
    chaos.cleanup_all.side_effect = cleanup
    juju.wait_for_unit_health.side_effect = health
    # WHEN the test succeeds, fails, or skips during stress
    if failure is None:
        module.test_live_cpu_stress_total(juju, MODEL, "app", timedelta(seconds=10), kubernetes)
        assert events == ["limit", "healthy", "stress", "unhealthy", "cleanup", "healthy", "restore"]
        juju.validate_model.assert_called_once()
    else:
        expected = (
            pytest.skip.Exception if failure == "skip" else (TimeoutError if failure == "status" else RuntimeError)
        )
        with pytest.raises(expected):
            module.test_live_cpu_stress_total(juju, MODEL, "app", timedelta(seconds=10), kubernetes)
        assert events[-2:] == ["cleanup", "restore"]
        juju.validate_model.assert_not_called()


@pytest.mark.parametrize("kubernetes", [False, True])
def test_unsupported_environment_skips_before_mutation(monkeypatch: pytest.MonkeyPatch, kubernetes: bool) -> None:
    juju = MagicMock(spec=JujuClient, backend=MagicMock())
    target = MagicMock() if kubernetes else None
    juju.backend.get_kubernetes_client_for_model.return_value = target
    monkeypatch.setattr(module, "available_chaos_tools", lambda _: set())
    with pytest.raises(pytest.skip.Exception):
        module.test_live_cpu_stress_total(juju, MODEL, "app", timedelta(seconds=10), target)
    if target is not None:
        target.get_charm_pods.assert_not_called()
    juju.idle_for_period.assert_not_called()
