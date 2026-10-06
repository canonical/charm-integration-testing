# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest
from juju import JujuBackend, JujuClient, JujuModelHandle, JujuRestartNotSupportedError
from kubernetes import client as K8sClient  # type: ignore[import-untyped]
from kubernetes_client import KubernetesClient
from test_suite import test_scale_ha as scale_ha
from test_suite import test_scale_remove_follower as remove_follower

from bundle_builder_x import Charm, CharmChannel


class RecordingJujuClient:
    def __init__(self, current_units: int) -> None:
        self.units = [f"target/{index}" for index in range(current_units)]
        self.calls: list[tuple[object, ...]] = []
        self.scale_callback: object | None = None

    def num_units(self, application: str, model: JujuModelHandle) -> int:
        self.calls.append(("num_units", application, model))
        return len(self.units)

    def scale_application(self, application: str, num: int, model: JujuModelHandle) -> None:
        self.calls.append(("scale_application", application, num, model))
        if num > len(self.units):
            next_index = max((int(unit.rsplit("/", maxsplit=1)[-1]) for unit in self.units), default=-1) + 1
            self.units.extend(
                f"{application}/{index}" for index in range(next_index, next_index + num - len(self.units))
            )
        elif num < len(self.units):
            self.units = sorted(self.units, key=lambda unit: int(unit.rsplit("/", maxsplit=1)[-1]))[:num]
        if callable(self.scale_callback):
            self.scale_callback(num)

    def application_units(self, application: str, model: JujuModelHandle) -> list[str]:
        return list(self.units)

    def remove_unit(self, unit: str, model: JujuModelHandle) -> None:
        self.calls.append(("remove_unit", unit, model))
        self.units.remove(unit)

    def idle_for_period(self, model: JujuModelHandle, timeout: timedelta | None = None) -> None:
        self.calls.append(("idle_for_period", model, timeout))

    def multi_model_idle_for_period(self, models: list[JujuModelHandle], timeout: timedelta | None = None) -> None:
        self.calls.append(("multi_model_idle_for_period", models, timeout))

    def validate_model(self, model: JujuModelHandle, level: str = "simple") -> None:
        self.calls.append(("validate_model", model, level))


class RecordingJujuBackend:
    def __init__(self, *, is_k8s: bool) -> None:
        self.is_k8s = is_k8s

    def is_k8s_model(self, model: JujuModelHandle) -> bool:
        return self.is_k8s


class RecordingKubernetesClient:
    def __init__(self, application: str, unit_count: int) -> None:
        self.application = application
        self.pods = [self._pod(index, f"uid-{index}") for index in range(unit_count)]
        self.calls: list[tuple[object, ...]] = []
        self._deleted_index: int | None = None

    def _pod(self, index: int, uid: str) -> K8sClient.V1Pod:
        pod = K8sClient.V1Pod()
        pod.metadata = K8sClient.V1ObjectMeta(
            name=f"{self.application}-{index}",
            uid=uid,
            labels={"apps.kubernetes.io/pod-index": str(index)},
        )
        return pod

    def scale_to(self, num_units: int) -> None:
        current_indices = {
            int(pod.metadata.labels["apps.kubernetes.io/pod-index"])
            for pod in self.pods
            if pod.metadata is not None and pod.metadata.labels is not None
        }
        self.pods = [
            pod
            for pod in self.pods
            if pod.metadata is not None
            and pod.metadata.labels is not None
            and int(pod.metadata.labels["apps.kubernetes.io/pod-index"]) < num_units
        ]
        for index in range(num_units):
            if index not in current_indices:
                self.pods.append(self._pod(index, f"uid-{index}"))

    def get_charm_pods(self, application_name: str, model: str) -> list[K8sClient.V1Pod]:
        return list(self.pods)

    def delete_pod(self, namespace: str, pod_name: str) -> None:
        pod = next(pod for pod in self.pods if pod.metadata is not None and pod.metadata.name == pod_name)
        assert pod.metadata is not None and pod.metadata.labels is not None
        self._deleted_index = int(pod.metadata.labels["apps.kubernetes.io/pod-index"])
        self.pods.remove(pod)
        self.calls.append(("delete_pod", namespace, pod_name))

    def wait_for_new_pod(
        self,
        application_name: str,
        namespace: str,
        existing_uids: set[str],
        timeout: timedelta,
    ) -> K8sClient.V1Pod:
        assert self._deleted_index is not None
        pod = self._pod(self._deleted_index, f"replacement-{self._deleted_index}")
        self.pods.append(pod)
        self.calls.append(("wait_for_new_pod", namespace, existing_uids, timeout))
        return pod

    def wait_for_pod_status(
        self,
        pod_name: str,
        namespace: str,
        target_status: object,
        timeout: timedelta,
    ) -> None:
        self.calls.append(("wait_for_pod_status", namespace, pod_name, target_status, timeout))


MODEL = JujuModelHandle(controller="controller", model="model")


def _charm(*, ha_units: int = 3, scale_down: bool = True, subordinate: bool = False) -> Charm:
    return Charm(
        name="mysql-k8s",
        channel=CharmChannel.model_validate("8.0/stable"),
        revision=1,
        ubuntu_version="22.04",
        ubuntu_arch="amd64",
        endpoints={},
        platforms=["kubernetes"],
        subordinate=subordinate,
        ha_units=ha_units,
        scale_down=scale_down,
    )


def _write_bundle(tmp_path: Path, *, application: str = "target", units: int = 2) -> Path:
    bundle = tmp_path / "bundle.yaml"
    bundle.write_text(f"applications:\n  {application}:\n    scale: {units}\n", encoding="utf-8")
    return bundle


def test_scale_to_ha_validates_immediately_when_application_is_already_large_enough() -> None:
    client = RecordingJujuClient(current_units=4)

    scale_ha.test_scale_to_ha(cast(JujuClient, client), MODEL, "target", _charm())

    assert client.calls == [
        ("num_units", "target", MODEL),
        ("validate_model", MODEL, "deep"),
    ]


def test_scale_to_ha_scales_waits_and_deep_validates() -> None:
    client = RecordingJujuClient(current_units=1)

    scale_ha.test_scale_to_ha(cast(JujuClient, client), MODEL, "target", _charm(ha_units=5))

    assert client.calls == [
        ("num_units", "target", MODEL),
        ("scale_application", "target", 5, MODEL),
        ("idle_for_period", MODEL, timedelta(minutes=15)),
        ("validate_model", MODEL, "deep"),
    ]


def test_scale_to_ha_skips_subordinate_before_accessing_juju() -> None:
    client = RecordingJujuClient(current_units=1)

    with pytest.raises(pytest.skip.Exception, match="mysql-k8s is subordinate"):
        scale_ha.test_scale_to_ha(
            cast(JujuClient, client),
            MODEL,
            "target",
            _charm(subordinate=True),
        )

    assert client.calls == []


def test_scale_from_ha_restores_original_units_waits_and_simple_validates(tmp_path: Path) -> None:
    client = RecordingJujuClient(current_units=3)

    scale_ha.test_scale_from_ha(
        cast(JujuClient, client),
        MODEL,
        _write_bundle(tmp_path),
        "target",
        "kubernetes",
        _charm(),
    )

    assert client.calls == [
        ("scale_application", "target", 2, MODEL),
        ("idle_for_period", MODEL, timedelta(minutes=15)),
        ("validate_model", MODEL, "simple"),
    ]


def test_scale_from_ha_skips_before_parsing_or_mutating_when_scaling_down_is_unsupported(tmp_path: Path) -> None:
    client = RecordingJujuClient(current_units=3)

    with pytest.raises(pytest.skip.Exception, match="mysql-k8s does not support scaling down"):
        scale_ha.test_scale_from_ha(
            cast(JujuClient, client),
            MODEL,
            tmp_path / "missing.yaml",
            "target",
            "kubernetes",
            _charm(scale_down=False),
        )

    assert client.calls == []


def test_scale_from_ha_skips_subordinate_before_parsing_or_accessing_juju(tmp_path: Path) -> None:
    client = RecordingJujuClient(current_units=3)

    with pytest.raises(pytest.skip.Exception, match="mysql-k8s is subordinate"):
        scale_ha.test_scale_from_ha(
            cast(JujuClient, client),
            MODEL,
            tmp_path / "missing.yaml",
            "target",
            "kubernetes",
            _charm(subordinate=True),
        )

    assert client.calls == []


def test_bundle_application_units_reads_platform_specific_unit_key(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle.yaml"
    bundle.write_text(
        "applications:\n"
        "  target:\n"
        "    scale: 2\n"
        "  machine-target:\n"
        "    num_units: 4\n"
        "---\n"
        "applications:\n"
        "  target:\n"
        "    offers: {}\n",
        encoding="utf-8",
    )

    assert scale_ha._bundle_application_units(bundle, "target", "kubernetes") == 2
    assert scale_ha._bundle_application_units(bundle, "machine-target", "machine") == 4


class FollowerClient(RecordingJujuClient):
    def __init__(self, *, error: str | None = None, current_units: int = 3) -> None:
        super().__init__(current_units)
        self.error = error

    def multi_model_idle_for_period(
        self, models: list[JujuModelHandle], timeout: timedelta, strict_timeout: bool
    ) -> None:
        self.calls.append(("idle", models, strict_timeout))
        if self.error == "recovery" and any(call[0] == "restart" for call in self.calls):
            raise TimeoutError("recovery")

    def restart_follower(self, application: str, model: JujuModelHandle, timeout: timedelta) -> str:
        self.calls.append(("restart", application, model))
        if self.error == "restart":
            raise RuntimeError("restart")
        if self.error == "unsupported":
            raise JujuRestartNotSupportedError("unsupported")
        if self.error == "not-implemented":
            raise NotImplementedError("restart hook failed")
        if self.error == "count":
            self.units.remove("target/0")
        return "target/1"

    def validate_model(self, model: JujuModelHandle, level: str = "simple") -> None:
        super().validate_model(model, level)
        if self.error == "validation":
            raise RuntimeError("validation")


def test_follower_restart_preserves_ha_without_requiring_scale_down() -> None:
    client = FollowerClient()
    neighbor = JujuModelHandle(controller="controller", model="neighbor")
    remove_follower.test_scale_remove_follower(
        cast(JujuClient, client), MODEL, neighbor, "target", _charm(scale_down=False)
    )
    assert client.calls == [
        ("idle", [MODEL, neighbor], True),
        ("num_units", "target", MODEL),
        ("restart", "target", MODEL),
        ("idle", [MODEL, neighbor], True),
        ("num_units", "target", MODEL),
        ("validate_model", MODEL, "simple"),
        ("validate_model", neighbor, "simple"),
    ]


@pytest.mark.parametrize("error", ["restart", "recovery", "validation", "count"])
def test_follower_failure_is_not_hidden(error: str) -> None:
    client = FollowerClient(error=error)
    with pytest.raises((RuntimeError, TimeoutError, pytest.fail.Exception)):
        remove_follower.test_scale_remove_follower(cast(JujuClient, client), MODEL, None, "target", _charm())
    if error != "validation":
        assert not any(call[0] == "validate_model" for call in client.calls)


def test_follower_requires_ha_count_before_disruption() -> None:
    client = FollowerClient(current_units=2)
    with pytest.raises(pytest.fail.Exception, match="at least 3"):
        remove_follower.test_scale_remove_follower(cast(JujuClient, client), MODEL, None, "target", _charm())
    assert not any(call[0] == "restart" for call in client.calls)


@pytest.mark.parametrize("reason", ["single", "subordinate", "unsupported"])
def test_follower_skips_unsupported_configuration(reason: str) -> None:
    client = FollowerClient(error=reason, current_units=1 if reason == "single" else 3)
    charm = _charm(ha_units=1 if reason == "single" else 3, subordinate=reason == "subordinate")
    with pytest.raises(pytest.skip.Exception):
        remove_follower.test_scale_remove_follower(cast(JujuClient, client), MODEL, None, "target", charm)
    assert not any(call[0] == "validate_model" for call in client.calls)


def test_follower_does_not_skip_unexpected_not_implemented_error() -> None:
    client = FollowerClient(error="not-implemented")
    with pytest.raises(NotImplementedError, match="restart hook failed"):
        remove_follower.test_scale_remove_follower(cast(JujuClient, client), MODEL, None, "target", _charm())
    assert not any(call[0] == "validate_model" for call in client.calls)


def test_unit_rotation_replaces_each_machine_unit_before_validation() -> None:
    client = RecordingJujuClient(current_units=3)

    scale_ha.test_unit_rotation(
        cast(JujuClient, client),
        cast(JujuBackend, RecordingJujuBackend(is_k8s=False)),
        None,
        MODEL,
        None,
        "target",
        _charm(),
    )

    assert [call for call in client.calls if call[0] == "scale_application"] == [
        ("scale_application", "target", 4, MODEL),
        ("scale_application", "target", 4, MODEL),
        ("scale_application", "target", 4, MODEL),
    ]
    assert [call[1] for call in client.calls if call[0] == "remove_unit"] == ["target/0", "target/1", "target/2"]
    validations = [call for call in client.calls if call[0] == "validate_model"]
    assert validations == [("validate_model", MODEL, "simple")] * 3
    assert client.units == ["target/3", "target/4", "target/5"]


def test_unit_rotation_replaces_kubernetes_pods_with_surge_capacity() -> None:
    client = RecordingJujuClient(current_units=2)
    kubernetes_client = RecordingKubernetesClient("target", 2)
    client.scale_callback = kubernetes_client.scale_to

    scale_ha.test_unit_rotation(
        cast(JujuClient, client),
        cast(JujuBackend, RecordingJujuBackend(is_k8s=True)),
        cast(KubernetesClient, kubernetes_client),
        MODEL,
        None,
        "target",
        _charm(ha_units=2),
    )

    assert [call for call in client.calls if call[0] == "scale_application"] == [
        ("scale_application", "target", 3, MODEL),
        ("scale_application", "target", 2, MODEL),
    ] * 2
    assert [call[2] for call in kubernetes_client.calls if call[0] == "delete_pod"] == [
        "target-0",
        "target-1",
    ]
    assert len([call for call in kubernetes_client.calls if call[0] == "wait_for_new_pod"]) == 2
    assert len([call for call in client.calls if call[0] == "validate_model"]) == 2
    assert client.units == ["target/0", "target/1"]
