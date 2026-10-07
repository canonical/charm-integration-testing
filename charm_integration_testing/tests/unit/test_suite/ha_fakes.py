# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from collections.abc import Callable
from datetime import timedelta

from juju import JujuModelHandle
from kubernetes import client as K8sClient  # type: ignore[import-untyped]
from kubernetes_client import KubernetesClient

from bundle_builder_x import Charm, CharmChannel

from ..extensions.shared import NullJujuBackend

MODEL = JujuModelHandle(controller="controller", model="model")


def charm(*, ha_units: int = 3, scale_down: bool = True, subordinate: bool = False) -> Charm:
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


class RecordingJujuClient:
    def __init__(self, current_units: int) -> None:
        self.units = [f"target/{index}" for index in range(current_units)]
        self.calls: list[tuple[object, ...]] = []
        self.scale_callback: object | None = None
        self.fail_next_scale_after_callback = False
        self.fail_scale_after_callback_on_call: int | None = None
        self.fail_next_multi_model_idle = False
        self.fail_multi_model_idle_on_call: int | None = None
        self.fail_next_remove_after_callback = False
        self.extra_units_on_scale_up = 0

    def num_units(self, application: str, model: JujuModelHandle) -> int:
        self.calls.append(("num_units", application, model))
        return len(self.units)

    def scale_application(self, application: str, num: int, model: JujuModelHandle) -> None:
        self.calls.append(("scale_application", application, num, model))
        scale_calls = len([call for call in self.calls if call[0] == "scale_application"])
        if num > len(self.units):
            next_index = max((int(unit.rsplit("/", maxsplit=1)[-1]) for unit in self.units), default=-1) + 1
            self.units.extend(
                f"{application}/{index}" for index in range(next_index, next_index + num - len(self.units))
            )
            self.units.extend(f"{application}/surge-extra-{index}" for index in range(self.extra_units_on_scale_up))
            self.extra_units_on_scale_up = 0
        elif num < len(self.units):
            self.units = sorted(self.units, key=lambda unit: int(unit.rsplit("/", maxsplit=1)[-1]))[:num]
        if callable(self.scale_callback):
            self.scale_callback(num)
        if self.fail_scale_after_callback_on_call == scale_calls:
            raise RuntimeError("scale cleanup hook failed")
        if self.fail_next_scale_after_callback:
            self.fail_next_scale_after_callback = False
            raise RuntimeError("scale extension failed")

    def application_units(self, application: str, model: JujuModelHandle) -> list[str]:
        return list(self.units)

    def remove_unit(self, unit: str, model: JujuModelHandle) -> None:
        self.calls.append(("remove_unit", unit, model))
        self.units.remove(unit)
        if self.fail_next_remove_after_callback:
            self.fail_next_remove_after_callback = False
            raise RuntimeError("unit removal hook failed")

    def idle_for_period(self, model: JujuModelHandle, timeout: timedelta | None = None) -> None:
        self.calls.append(("idle_for_period", model, timeout))

    def multi_model_idle_for_period(self, models: list[JujuModelHandle], timeout: timedelta | None = None) -> None:
        self.calls.append(("multi_model_idle_for_period", models, timeout))
        idle_calls = len([call for call in self.calls if call[0] == "multi_model_idle_for_period"])
        if self.fail_next_multi_model_idle:
            self.fail_next_multi_model_idle = False
            raise TimeoutError("model failed to become idle")
        if self.fail_multi_model_idle_on_call == idle_calls:
            raise TimeoutError("cleanup idle wait failed")

    def validate_model(self, model: JujuModelHandle, level: str = "simple") -> None:
        self.calls.append(("validate_model", model, level))


class RecordingJujuBackend(NullJujuBackend):
    def __init__(self, *, k8s_model: bool, kubernetes_client: KubernetesClient | None = None) -> None:
        self.is_k8s = k8s_model
        self.kubernetes_client = kubernetes_client

    def is_k8s_model(self, model: JujuModelHandle) -> bool:
        return self.is_k8s

    def get_kubernetes_client_for_model(self, model: JujuModelHandle) -> KubernetesClient | None:
        return self.kubernetes_client


class RecordingKubernetesClient:
    def __init__(self, application: str, unit_count: int, *, statefulset: bool = True) -> None:
        self.application = application
        self.statefulset = statefulset
        self._pod_name_counter = 0
        self.pods = [self._pod(index, f"uid-{index}") for index in range(unit_count)]
        self.calls: list[tuple[object, ...]] = []
        self._deleted_index: int | None = None
        self.scale_down_uid: str | None = None
        self.defer_next_scale_down = False
        self.fail_wait_for_new_pod = False
        self.fail_wait_for_pod_ready = False

    def _pod(self, index: int, uid: str) -> K8sClient.V1Pod:
        pod = K8sClient.V1Pod()
        self._pod_name_counter += 1
        pod_name = (
            f"{self.application}-{index}"
            if self.statefulset
            else f"{self.application}-hash-pod-{self._pod_name_counter}"
        )
        labels = {"apps.kubernetes.io/pod-index": str(index)} if self.statefulset else {}
        pod.metadata = K8sClient.V1ObjectMeta(
            name=pod_name,
            uid=uid,
            labels=labels,
        )
        return pod

    def scale_to(self, num_units: int) -> None:
        if num_units < len(self.pods) and self.defer_next_scale_down:
            self.defer_next_scale_down = False
            return
        while len(self.pods) > num_units:
            pod = next(
                (pod for pod in self.pods if pod.metadata is not None and pod.metadata.uid == self.scale_down_uid),
                self.pods[-1],
            )
            self.pods.remove(pod)
            self.scale_down_uid = None
        while len(self.pods) < num_units:
            next_index = (
                max(
                    (
                        self._index_for_uid(pod.metadata.uid)
                        for pod in self.pods
                        if pod.metadata is not None and pod.metadata.uid is not None
                    ),
                    default=-1,
                )
                + 1
            )
            self.pods.append(self._pod(next_index, f"scaled-{next_index}"))

    def _index_for_uid(self, uid: str | None) -> int:
        if uid is None:
            return -1
        if uid.startswith("replacement-"):
            return int(uid.removeprefix("replacement-"))
        return int(uid.removeprefix("uid-").removeprefix("scaled-"))

    def get_charm_pods(self, application_name: str, model: str) -> list[K8sClient.V1Pod]:
        return list(self.pods)

    def delete_pod(self, namespace: str, pod_name: str) -> None:
        pod = next(pod for pod in self.pods if pod.metadata is not None and pod.metadata.name == pod_name)
        assert pod.metadata is not None
        self._deleted_index = self._index_for_uid(pod.metadata.uid)
        self.pods.remove(pod)
        self.calls.append(("delete_pod", namespace, pod_name))

    def wait_for_new_pod(
        self,
        application_name: str,
        namespace: str,
        existing_uids: set[str],
        timeout: timedelta,
    ) -> K8sClient.V1Pod:
        if self.fail_wait_for_new_pod:
            raise TimeoutError("replacement pod did not appear")
        assert self._deleted_index is not None
        pod = self._pod(self._deleted_index, f"replacement-{self._deleted_index}")
        self.pods.append(pod)
        self.calls.append(("wait_for_new_pod", namespace, existing_uids, timeout))
        return pod

    def wait_for_pod_ready(self, pod_name: str, namespace: str, timeout: timedelta) -> None:
        self.calls.append(("wait_for_pod_ready", namespace, pod_name, timeout))
        if self.fail_wait_for_pod_ready:
            raise TimeoutError("replacement pod did not become ready")

    def wait_for_charm_pods_ready(
        self,
        application_name: str,
        namespace: str,
        expected_count: int,
        timeout: timedelta,
    ) -> list[K8sClient.V1Pod]:
        self.calls.append(("wait_for_charm_pods_ready", application_name, namespace, expected_count, timeout))
        if len(self.pods) > expected_count and self.scale_down_uid is not None:
            pod = next(pod for pod in self.pods if pod.metadata is not None and pod.metadata.uid == self.scale_down_uid)
            self.pods.remove(pod)
            self.scale_down_uid = None
        if len(self.pods) != expected_count:
            raise TimeoutError(f"expected {expected_count} ready pods")
        return list(self.pods)

    def wait(
        self,
        check: Callable[[], list[K8sClient.V1Pod] | None],
        timeout_message: str,
        timeout: timedelta,
    ) -> list[K8sClient.V1Pod]:
        self.calls.append(("wait", timeout_message, timeout))
        result = check()
        if result is None and self.scale_down_uid is not None:
            pod = next(pod for pod in self.pods if pod.metadata is not None and pod.metadata.uid == self.scale_down_uid)
            self.pods.remove(pod)
            self.scale_down_uid = None
            result = check()
        if result is None:
            raise TimeoutError(timeout_message)
        return result
