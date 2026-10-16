# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast

import pytest
from juju import JujuModelHandle, JujuWaitState
from juju_cmd import JujuCmdBackend
from juju_jubilant.backend import JubilantBackend
from juju_jubilant.client import JubilantClient
from kubernetes_client import KubernetesClient

MODEL = JujuModelHandle(controller="controller", model="model")


@dataclass
class UnitOperationsClient:
    units: dict[str, Any] = field(default_factory=dict)

    def model(self, model: JujuModelHandle) -> "UnitOperationsClient":
        return self

    def status(self) -> Any:
        return SimpleNamespace(apps={"target": SimpleNamespace(units=self.units)})


class RebootKubernetesClient:
    def __init__(self, pods: list[Any] | None = None) -> None:
        self.pods = (
            pods
            if pods is not None
            else [
                SimpleNamespace(
                    metadata=SimpleNamespace(
                        annotations={"unit.juju.is/id": "target/1"},
                        deletion_timestamp=None,
                        name="target-1",
                    )
                )
            ]
        )
        self.deleted_pods: list[tuple[str, str]] = []

    def get_charm_pods(self, application: str, namespace: str) -> list[Any]:
        assert (application, namespace) == ("target", MODEL.model)
        return self.pods

    def delete_pod(self, namespace: str, pod_name: str) -> None:
        self.deleted_pods.append((namespace, pod_name))


class UnitRebootBackend(JubilantBackend):
    def __init__(self, kubernetes: RebootKubernetesClient | None = None) -> None:
        super().__init__(client=cast(JubilantClient, UnitOperationsClient()))
        self.kubernetes = kubernetes
        self.ssh_calls: list[tuple[JujuModelHandle, str, str]] = []

    def kubernetes_cloud_for_model(self, model: JujuModelHandle) -> str | None:
        return "test-cloud" if self.kubernetes is not None else None

    def _kubernetes_client_for_cloud(self, cloud: str) -> KubernetesClient:
        assert cloud == "test-cloud"
        return cast(KubernetesClient, self.kubernetes)

    def ssh(
        self, model: JujuModelHandle, application: str, command: str, *, timeout: float | None = None
    ) -> str | None:
        self.ssh_calls.append((model, application, command))
        return None


def test_application_leader_returns_unit_marked_as_leader() -> None:
    client = UnitOperationsClient(
        units={
            "target/0": SimpleNamespace(leader=False),
            "target/1": SimpleNamespace(leader=True),
        }
    )

    backend = JubilantBackend(client=cast(JubilantClient, client))
    assert backend.application_leader(MODEL, "target") == "target/1"


def test_application_leader_raises_when_no_leader_is_reported() -> None:
    client = UnitOperationsClient(units={"target/0": SimpleNamespace(leader=False)})

    with pytest.raises(RuntimeError, match="Expected one leader"):
        JubilantBackend(client=cast(JubilantClient, client)).application_leader(MODEL, "target")


def test_unit_operations_do_not_add_abstract_requirements_to_legacy_backend() -> None:
    assert not {
        "application_leader",
        "remove_unit",
        "reboot_unit",
        "wait_for_unit_removal",
        "wait_for_unit_unavailable",
        "wait_for_unit_available",
    }.intersection(JujuCmdBackend.__abstractmethods__)


def test_reboot_unit_uses_ssh_for_machine_models() -> None:
    backend = UnitRebootBackend()

    backend.reboot_unit(MODEL, "target/1")

    assert backend.ssh_calls == [
        (MODEL, "target/1", "sudo -n systemd-run --quiet --collect --on-active=3s /usr/bin/systemctl reboot")
    ]


def test_reboot_unit_deletes_the_target_kubernetes_unit_pod() -> None:
    kubernetes = RebootKubernetesClient()
    backend = UnitRebootBackend(kubernetes)

    backend.reboot_unit(MODEL, "target/1")

    assert kubernetes.deleted_pods == [(MODEL.model, "target-1")]
    assert backend.ssh_calls == []


def test_reboot_unit_rejects_ambiguous_kubernetes_pod() -> None:
    pods = [
        SimpleNamespace(
            metadata=SimpleNamespace(
                annotations={"unit.juju.is/id": "target/1"},
                deletion_timestamp=None,
                name="target-1",
            )
        ),
        SimpleNamespace(
            metadata=SimpleNamespace(
                annotations={"unit.juju.is/id": "target/1"},
                deletion_timestamp=None,
                name="target-1-duplicate",
            )
        ),
    ]
    kubernetes = RebootKubernetesClient(pods)
    backend = UnitRebootBackend(kubernetes)

    with pytest.raises(RuntimeError, match="single live Pod"):
        backend.reboot_unit(MODEL, "target/1")

    assert kubernetes.deleted_pods == []


def test_wait_for_unit_removal_checks_status(monkeypatch: pytest.MonkeyPatch) -> None:
    client = UnitOperationsClient(units={"target/0": SimpleNamespace(leader=False)})
    backend = JubilantBackend(client=cast(JubilantClient, client))
    ready_checks: list[tuple[bool, JujuWaitState]] = []

    def record_wait(model: JujuModelHandle, ready: Any, timeout: Any = None) -> None:
        ready_checks.append(ready(client.status()))

    monkeypatch.setattr(backend, "wait", record_wait)
    backend.wait_for_unit_removal(MODEL, "target/1", timeout=None)

    assert len(ready_checks) == 1
    assert ready_checks[0][0] is True
    assert ready_checks[0][1].message == "waiting for removal of unit 'target/1'"


@pytest.mark.parametrize(("agent_status", "expected"), [("down", True), ("lost", True), ("idle", False)])
def test_wait_for_unit_unavailable_checks_agent_status(
    monkeypatch: pytest.MonkeyPatch, agent_status: str, expected: bool
) -> None:
    client = UnitOperationsClient(
        units={"target/1": SimpleNamespace(juju_status=SimpleNamespace(current=agent_status))}
    )
    backend = JubilantBackend(client=cast(JubilantClient, client))
    ready_checks: list[tuple[bool, JujuWaitState]] = []

    def record_wait(model: JujuModelHandle, ready: Any, timeout: Any = None) -> None:
        ready_checks.append(ready(client.status()))

    monkeypatch.setattr(backend, "wait", record_wait)
    backend.wait_for_unit_unavailable(MODEL, "target/1", timeout=None)

    assert ready_checks[0][0] is expected
    assert ready_checks[0][1].message == "waiting for unit 'target/1' to become unavailable"


@pytest.mark.parametrize(
    ("workload_status", "agent_status", "expected"),
    [("active", "idle", True), ("active", "executing", False), ("maintenance", "idle", False)],
)
def test_wait_for_unit_available_checks_workload_and_agent_status(
    monkeypatch: pytest.MonkeyPatch, workload_status: str, agent_status: str, expected: bool
) -> None:
    client = UnitOperationsClient(
        units={
            "target/1": SimpleNamespace(
                workload_status=SimpleNamespace(current=workload_status),
                juju_status=SimpleNamespace(current=agent_status),
            )
        }
    )
    backend = JubilantBackend(client=cast(JubilantClient, client))
    ready_checks: list[tuple[bool, JujuWaitState]] = []

    def record_wait(model: JujuModelHandle, ready: Any, timeout: Any = None) -> None:
        ready_checks.append(ready(client.status()))

    monkeypatch.setattr(backend, "wait", record_wait)
    backend.wait_for_unit_available(MODEL, "target/1", timeout=None)

    assert ready_checks[0][0] is expected
    assert ready_checks[0][1].message == "waiting for unit 'target/1' to become available"
