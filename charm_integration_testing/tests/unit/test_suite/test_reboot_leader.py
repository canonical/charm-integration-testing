# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta
from typing import Any, Callable, cast

import pytest
from juju import JujuClient, JujuModelHandle
from kubernetes import client as K8sClient  # type: ignore[import-untyped]
from kubernetes_client import KubernetesClient, PodStatus
from test_suite import test_reboot_leader as reboot_leader

from bundle_builder_x import Charm, CharmChannel

MODEL = JujuModelHandle(controller="controller", model="model")
NEIGHBOR_MODEL = JujuModelHandle(controller="neighbor-controller", model="neighbor-model")


def _charm(*, ha_units: int = 3, subordinate: bool = False) -> Charm:
    return Charm(
        name="mysql-k8s",
        channel=CharmChannel.model_validate("8.0/stable"),
        revision=1,
        ubuntu_version="22.04",
        ubuntu_arch="amd64",
        endpoints={},
        platforms=["machine"],
        subordinate=subordinate,
        ha_units=ha_units,
    )


class RecordingJujuClient:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def num_units(self, application: str, model: JujuModelHandle) -> int:
        self.calls.append(("num_units", application, model))
        return 3

    def application_leader(self, application: str, model: JujuModelHandle) -> str:
        self.calls.append(("application_leader", application, model))
        return f"{application}/1"

    def ssh(self, unit: str, command: str, model: JujuModelHandle) -> None:
        self.calls.append(("ssh", unit, command, model))

    def validate_model(
        self, model: JujuModelHandle, level: str = "simple", *, applications: list[str] | None = None
    ) -> None:
        self.calls.append(("validate_model", model, level, applications))

    def multi_model_idle_for_period(self, models: list[JujuModelHandle], timeout: timedelta | None = None) -> None:
        self.calls.append(("multi_model_idle_for_period", models, timeout))


def _pod(name: str, uid: str, unit: str) -> K8sClient.V1Pod:
    return K8sClient.V1Pod(
        metadata=K8sClient.V1ObjectMeta(name=name, uid=uid, annotations={"unit.juju.is/id": unit}),
        status=K8sClient.V1PodStatus(phase="Running"),
    )


class RecordingKubernetesClient:
    def __init__(self) -> None:
        self.pods = [_pod("target-0", "uid-0", "target/0"), _pod("target-1", "uid-1", "target/1")]
        self.calls: list[tuple[object, ...]] = []

    def get_charm_pods(self, application_name: str, model: str) -> list[K8sClient.V1Pod]:
        assert application_name == "target"
        assert model == MODEL.model
        return self.pods

    def delete_pod(self, namespace: str, pod_name: str) -> None:
        self.calls.append(("delete_pod", namespace, pod_name))
        self.pods = [pod for pod in self.pods if pod.metadata.name != pod_name]
        self.pods.append(_pod("target-2", "uid-unrelated", "target/0"))

    def wait(
        self, check: Callable[[], Any], timeout_message: str, timeout: timedelta | None = None
    ) -> Any:
        self.calls.append(("wait", timeout_message, timeout))
        if "was not removed" in timeout_message:
            return check()

        assert check() is None
        replacement = _pod("target-1", "uid-replacement", "target/1")
        self.pods.append(replacement)
        assert check() is replacement
        return replacement

    def wait_for_pod_status(
        self, pod_name: str, namespace: str, target_status: PodStatus, timeout: timedelta | None = None
    ) -> None:
        self.calls.append(("wait_for_pod_status", pod_name, namespace, target_status, timeout))


def test_reboot_leader_validates_before_waiting_for_recovery() -> None:
    client = RecordingJujuClient()

    reboot_leader.test_reboot_leader(
        cast(JujuClient, client),
        MODEL,
        "target",
        "machine",
        _charm(),
        None,
        None,
        None,
    )

    assert client.calls == [
        ("num_units", "target", MODEL),
        ("application_leader", "target", MODEL),
        ("ssh", "target/1", "sudo reboot", MODEL),
        ("validate_model", MODEL, "deep", ["target"]),
        ("multi_model_idle_for_period", [MODEL], timedelta(minutes=15)),
    ]


def test_reboot_leader_waits_for_recovery_when_validation_fails() -> None:
    validation_error = RuntimeError("validation failed")

    class FailingValidationClient(RecordingJujuClient):
        def validate_model(
            self, model: JujuModelHandle, level: str = "simple", *, applications: list[str] | None = None
        ) -> None:
            super().validate_model(model, level, applications=applications)
            raise validation_error

    client = FailingValidationClient()

    with pytest.raises(RuntimeError, match="validation failed"):
        reboot_leader.test_reboot_leader(
            cast(JujuClient, client),
            MODEL,
            "target",
            "machine",
            _charm(),
            None,
            None,
            None,
        )

    assert client.calls[-1] == ("multi_model_idle_for_period", [MODEL], timedelta(minutes=15))


def test_reboot_leader_kubernetes_validates_both_models_and_waits_for_leader_recovery() -> None:
    juju = RecordingJujuClient()
    kubernetes = RecordingKubernetesClient()

    reboot_leader.test_reboot_leader(
        cast(JujuClient, juju),
        MODEL,
        "target",
        "kubernetes",
        _charm(),
        NEIGHBOR_MODEL,
        "neighbor",
        cast(KubernetesClient, kubernetes),
    )

    assert kubernetes.calls == [
        ("delete_pod", MODEL.model, "target-1"),
        ("wait", "Leader Pod target-1 was not removed.", timedelta(minutes=15)),
        ("wait", "Replacement Pod for leader target/1 did not appear.", timedelta(minutes=15)),
        ("wait_for_pod_status", "target-1", MODEL.model, PodStatus.RUNNING, timedelta(minutes=15)),
    ]
    assert juju.calls == [
        ("num_units", "target", MODEL),
        ("application_leader", "target", MODEL),
        ("validate_model", MODEL, "deep", ["target"]),
        ("validate_model", NEIGHBOR_MODEL, "deep", ["neighbor"]),
        ("multi_model_idle_for_period", [MODEL, NEIGHBOR_MODEL], timedelta(minutes=15)),
    ]
