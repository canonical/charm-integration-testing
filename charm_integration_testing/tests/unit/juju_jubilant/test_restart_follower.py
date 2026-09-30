# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import subprocess
from dataclasses import replace
from datetime import timedelta
from typing import cast

import jubilant
import pytest
from jubilant.statustypes import AppStatus, ModelStatus, UnitStatus
from juju import JujuClient, JujuExtension, JujuModelHandle
from juju.backend import JujuBackend
from juju_cmd import JujuCmdBackend
from juju_jubilant.backend import JubilantBackend
from juju_jubilant.client import JubilantClient
from kubernetes.client import V1ObjectMeta, V1Pod  # type: ignore[import-untyped]
from kubernetes_client import KubernetesClient

MODEL = JujuModelHandle(controller="controller", model="model")
TIMEOUT = timedelta(seconds=10)
BOOT = "11111111-1111-1111-1111-111111111111"
NEW_BOOT = "22222222-2222-2222-2222-222222222222"


def status() -> jubilant.Status:
    return jubilant.Status(
        model=ModelStatus(name="model", type="iaas", controller="controller", cloud="cloud", version="3.6"),
        machines={},
        apps={
            "target": AppStatus(
                charm="target",
                charm_origin="charmhub",
                charm_name="target",
                charm_rev=1,
                exposed=False,
                units={
                    "target/0": UnitStatus(leader=True, machine="0"),
                    "target/1": UnitStatus(machine="1"),
                    "target/2": UnitStatus(machine="2"),
                },
            )
        },
    )


class KubernetesStub:
    def __init__(self) -> None:
        self.pods = [
            V1Pod(metadata=V1ObjectMeta(name="target-1", uid="old", annotations={"unit.juju.is/id": "target/1"}))
        ]
        self.calls: list[tuple[object, ...]] = []

    def get_charm_pods(self, application: str, namespace: str) -> list[V1Pod]:
        return self.pods

    def restart_pod(self, namespace: str, name: str, uid: str, timeout: timedelta) -> None:
        self.calls.append((namespace, name, uid, timeout))


class ClientStub(JubilantClient):
    def __init__(self) -> None:
        self.boots: list[str | Exception] = [BOOT, NEW_BOOT]
        self.calls: list[tuple[str, str]] = []
        self.timeouts: list[float] = []

    def ssh(self, model: JujuModelHandle, machine: str, command: str, timeout: float) -> str:
        self.calls.append((machine, command))
        self.timeouts.append(timeout)
        if command.startswith("cat "):
            result = self.boots.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return ""


class BackendStub(JubilantBackend):
    def __init__(self, kube: KubernetesStub | None = None) -> None:
        self.stub = ClientStub()
        super().__init__(client=self.stub)
        self.snapshots = [status(), status()]
        self.kube = kube

    def status(self, model: JujuModelHandle) -> jubilant.Status:
        return self.snapshots.pop(0)

    def get_kubernetes_client_for_model(self, model: JujuModelHandle) -> KubernetesClient | None:
        return cast(KubernetesClient | None, self.kube)


def test_restarts_only_follower_pod() -> None:
    kube = KubernetesStub()
    backend = BackendStub(kube)
    assert backend.restart_follower(MODEL, "target", TIMEOUT) == "target/1"
    assert kube.calls == [("model", "target-1", "old", TIMEOUT)]


@pytest.mark.parametrize("change", ["leader", "membership", "no-leader"])
def test_rechecks_leadership_before_deletion(change: str) -> None:
    kube = KubernetesStub()
    backend = BackendStub(kube)
    units = backend.snapshots[1].apps["target"].units
    if change == "leader":
        units["target/0"] = replace(units["target/0"], leader=False)
        units["target/1"] = replace(units["target/1"], leader=True)
    elif change == "membership":
        del units["target/2"]
    else:
        units["target/0"] = replace(units["target/0"], leader=False)
    with pytest.raises(RuntimeError, match="changed"):
        backend.restart_follower(MODEL, "target", TIMEOUT)
    assert kube.calls == []


@pytest.mark.parametrize("leaders", [0, 2])
def test_rejects_ambiguous_leadership(leaders: int) -> None:
    backend = BackendStub(KubernetesStub())
    units = backend.snapshots[0].apps["target"].units
    for index, name in enumerate(units):
        units[name] = replace(units[name], leader=index < leaders)
    with pytest.raises(RuntimeError, match="exactly one"):
        backend.restart_follower(MODEL, "target", TIMEOUT)


def test_missing_target_pod_does_not_delete_another_unit() -> None:
    kube = KubernetesStub()
    kube.pods[0].metadata.annotations = {"unit.juju.is/id": "target/0"}
    with pytest.raises(RuntimeError, match="single live Pod"):
        BackendStub(kube).restart_follower(MODEL, "target", TIMEOUT)
    assert kube.calls == []


def test_machine_reboot_waits_for_changed_boot_id(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = BackendStub()
    backend.stub.boots = [BOOT, BOOT, NEW_BOOT]
    monkeypatch.setattr("juju_jubilant.backend.time.sleep", lambda _: None)
    assert backend.restart_follower(MODEL, "target", TIMEOUT) == "target/1"
    assert all(target == "1" for target, _ in backend.stub.calls)
    assert len(backend.stub.calls) == 4
    assert "systemctl reboot" in backend.stub.calls[1][1]


@pytest.mark.parametrize("placement", ["1", "1/lxd/0"])
def test_shared_machine_is_not_rebooted(placement: str) -> None:
    backend = BackendStub()
    backend.snapshots[0].apps["other"] = replace(
        backend.snapshots[0].apps["target"], units={"other/0": UnitStatus(machine=placement)}
    )
    with pytest.raises(NotImplementedError, match="also restart"):
        backend.restart_follower(MODEL, "target", TIMEOUT)
    assert backend.stub.calls == []


def test_machine_without_reboot_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = BackendStub()
    ticks = iter([0.0, 0.0, 0.0, 11.0])
    monkeypatch.setattr("juju_jubilant.backend.time.monotonic", lambda: next(ticks))
    with pytest.raises(TimeoutError, match="did not reboot"):
        backend.restart_follower(MODEL, "target", TIMEOUT)


def test_placement_change_prevents_reboot() -> None:
    backend = BackendStub()
    units = backend.snapshots[1].apps["target"].units
    units["target/1"] = replace(units["target/1"], machine="3")
    with pytest.raises(RuntimeError, match="placement changed"):
        backend.restart_follower(MODEL, "target", TIMEOUT)
    assert len(backend.stub.calls) == 1


def test_nested_container_is_not_rebooted() -> None:
    backend = BackendStub()
    units = backend.snapshots[0].apps["target"].units
    units["target/1"] = replace(units["target/1"], machine="1/lxd/0")
    with pytest.raises(NotImplementedError, match="nested container"):
        backend.restart_follower(MODEL, "target", TIMEOUT)
    assert backend.stub.calls == []


def test_ssh_failure_during_reboot_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = BackendStub()
    backend.stub.boots = [BOOT, jubilant.CLIError(1, ["ssh"], "", "disconnected"), NEW_BOOT]
    monkeypatch.setattr("juju_jubilant.backend.time.sleep", lambda _: None)
    assert backend.restart_follower(MODEL, "target", TIMEOUT) == "target/1"


def test_invalid_boot_id_prevents_reboot() -> None:
    backend = BackendStub()
    backend.stub.boots = [""]
    with pytest.raises(RuntimeError, match="Invalid boot ID"):
        backend.restart_follower(MODEL, "target", TIMEOUT)
    assert len(backend.stub.calls) == 1


def test_client_delegates_without_adding_an_abstract_backend_requirement() -> None:
    kube = KubernetesStub()
    client = JujuClient(BackendStub(kube), logging.getLogger(__name__))
    assert client.restart_follower("target", MODEL, TIMEOUT) == "target/1"
    assert "restart_follower" not in JujuCmdBackend.__abstractmethods__
    with pytest.raises(NotImplementedError, match="does not support"):
        JujuBackend.restart_follower(client.backend, MODEL, "target", TIMEOUT)


class RestartExtension(JujuExtension):
    def __init__(self, kube: KubernetesStub, *, fail: bool = False) -> None:
        self.kube = kube
        self.fail = fail
        self.calls: list[tuple[JujuModelHandle, str]] = []

    def post_restart_unit(self, model: JujuModelHandle, unit: str) -> None:
        assert self.kube.calls == [("model", "target-1", "old", TIMEOUT)]
        self.calls.append((model, unit))
        if self.fail:
            raise RuntimeError("restart hook failed")


def test_client_runs_hook_after_restart() -> None:
    kube = KubernetesStub()
    extension = RestartExtension(kube)
    client = JujuClient(BackendStub(kube), logging.getLogger(__name__), [extension])
    client.restart_follower("target", MODEL, TIMEOUT)
    assert extension.calls == [(MODEL, "target/1")]


def test_restart_failure_does_not_run_hook() -> None:
    kube = KubernetesStub()
    kube.pods = []
    extension = RestartExtension(kube)
    client = JujuClient(BackendStub(kube), logging.getLogger(__name__), [extension])
    with pytest.raises(RuntimeError, match="single live Pod"):
        client.restart_follower("target", MODEL, TIMEOUT)
    assert extension.calls == []


def test_hook_failure_is_not_hidden() -> None:
    kube = KubernetesStub()
    client = JujuClient(BackendStub(kube), logging.getLogger(__name__), [RestartExtension(kube, fail=True)])
    with pytest.raises(RuntimeError, match="restart hook failed"):
        client.restart_follower("target", MODEL, TIMEOUT)


def test_each_ssh_call_uses_remaining_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = BackendStub()
    ticks = iter([0.0, 1.0, 2.0, 3.0, 4.0])
    monkeypatch.setattr("juju_jubilant.backend.time.monotonic", lambda: next(ticks))
    backend.restart_follower(MODEL, "target", TIMEOUT)
    assert backend.stub.timeouts == [9.0, 8.0, 6.0]


def test_ssh_timeout_during_reboot_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = BackendStub()
    backend.stub.boots = [BOOT, subprocess.TimeoutExpired("juju ssh", 1), NEW_BOOT]
    monkeypatch.setattr("juju_jubilant.backend.time.sleep", lambda _: None)
    assert backend.restart_follower(MODEL, "target", TIMEOUT) == "target/1"


def test_ssh_timeout_before_reboot_fails() -> None:
    backend = BackendStub()
    backend.stub.boots = [subprocess.TimeoutExpired("juju ssh", 1)]
    with pytest.raises(subprocess.TimeoutExpired):
        backend.restart_follower(MODEL, "target", TIMEOUT)
    assert len(backend.stub.calls) == 1
