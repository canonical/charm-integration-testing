# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from datetime import datetime, timedelta, timezone
from typing import cast

import pytest
from juju import JujuClient, JujuModelHandle
from juju.extension import JujuExtension
from kubernetes_client import KubernetesClient

from validators.base import ValidationResult

from .ha_fakes import MODEL, RecordingJujuBackend, RecordingJujuClient, RecordingKubernetesClient


class RecordingRotationJujuClient(JujuClient):
    def __init__(
        self,
        recorder: RecordingJujuClient,
        backend: RecordingJujuBackend,
        *,
        extensions: list[JujuExtension] | None = None,
    ) -> None:
        super().__init__(backend, logging.getLogger(__name__), extensions)
        self.recorder = recorder

    def application_units(self, application: str, model: JujuModelHandle) -> list[str]:
        return self.recorder.application_units(application, model)

    def scale_application(self, application: str, num: int, model: JujuModelHandle) -> None:
        self.recorder.scale_application(application, num, model)

    def remove_unit(self, unit: str, model: JujuModelHandle) -> None:
        self.recorder.remove_unit(unit, model)

    def multi_model_idle_for_period(
        self,
        models: list[JujuModelHandle],
        timeout: timedelta | None = None,
        count: int = 10,
        strict_timeout: bool = False,
    ) -> None:
        self.recorder.multi_model_idle_for_period(models, timeout)

    def validate_model(
        self,
        model: JujuModelHandle,
        level: str | None = "simple",
        *,
        applications: list[str] | None = None,
        endpoints: set[tuple[str, str]] | None = None,
    ) -> dict[str, list[ValidationResult]]:
        self.recorder.validate_model(model, level or "simple")
        return {}


class UnsupportedTargetedRemovalBackend(RecordingJujuBackend):
    def supports_targeted_unit_removal(self) -> bool:
        return False


def _rotate_units(
    client: RecordingJujuClient,
    *,
    k8s_model: bool,
    kubernetes_client: RecordingKubernetesClient | None = None,
) -> None:
    backend = RecordingJujuBackend(
        k8s_model=k8s_model,
        kubernetes_client=cast(KubernetesClient | None, kubernetes_client),
    )
    juju_client = RecordingRotationJujuClient(client, backend)
    juju_client.rotate_application_units("target", MODEL)


def test_unit_rotation_replaces_each_machine_unit_before_validation() -> None:
    client = RecordingJujuClient(current_units=3)

    _rotate_units(client, k8s_model=False)

    assert [call for call in client.calls if call[0] == "scale_application"] == [
        ("scale_application", "target", 4, MODEL),
        ("scale_application", "target", 4, MODEL),
        ("scale_application", "target", 4, MODEL),
    ]
    assert [call[1] for call in client.calls if call[0] == "remove_unit"] == ["target/0", "target/1", "target/2"]
    assert [call for call in client.calls if call[0] == "validate_model"] == [
        ("validate_model", MODEL, "simple"),
        ("validate_model", MODEL, "simple"),
        ("validate_model", MODEL, "simple"),
        ("validate_model", MODEL, "deep"),
    ]
    assert client.calls[-1] == ("validate_model", MODEL, "deep")
    assert client.units == ["target/3", "target/4", "target/5"]


def test_rotation_requires_a_kubeconfig_for_kubernetes_models() -> None:
    client = RecordingJujuClient(current_units=2)

    with pytest.raises(RuntimeError, match="No kubeconfig configured"):
        _rotate_units(client, k8s_model=True)

    assert not [call for call in client.calls if call[0] == "scale_application"]
    assert client.units == ["target/0", "target/1"]


def test_rotation_rejects_machine_backend_without_targeted_unit_removal() -> None:
    client = RecordingJujuClient(current_units=2)
    backend = UnsupportedTargetedRemovalBackend(k8s_model=False)
    juju_client = RecordingRotationJujuClient(client, backend)

    with pytest.raises(NotImplementedError, match="targeted unit removal is not supported"):
        juju_client.rotate_application_units("target", MODEL)

    assert not [call for call in client.calls if call[0] == "scale_application"]
    assert client.units == ["target/0", "target/1"]


def test_rotation_rejects_applications_without_units() -> None:
    client = RecordingJujuClient(current_units=0)

    with pytest.raises(ValueError, match="it has no units"):
        _rotate_units(client, k8s_model=False)


def test_unit_rotation_removes_machine_surge_after_failure() -> None:
    client = RecordingJujuClient(current_units=3)
    client.fail_next_multi_model_idle = True

    with pytest.raises(TimeoutError, match="model failed to become idle"):
        _rotate_units(client, k8s_model=False)

    assert [call for call in client.calls if call[0] == "remove_unit"] == [("remove_unit", "target/3", MODEL)]
    assert client.units == ["target/0", "target/1", "target/2"]


def test_unit_rotation_removes_machine_surge_when_post_scale_hook_fails() -> None:
    client = RecordingJujuClient(current_units=3)
    client.fail_next_scale_after_callback = True

    with pytest.raises(RuntimeError, match="scale extension failed"):
        _rotate_units(client, k8s_model=False)

    assert [call for call in client.calls if call[0] == "remove_unit"] == [("remove_unit", "target/3", MODEL)]
    assert client.units == ["target/0", "target/1", "target/2"]


def test_unit_rotation_settles_before_cleanup_when_scale_raises_before_mutation() -> None:
    client = RecordingJujuClient(current_units=3)
    client.fail_next_scale_before_mutation = True

    with pytest.raises(RuntimeError, match="scale failed before mutation"):
        _rotate_units(client, k8s_model=False)

    assert [call for call in client.calls if call[0] == "multi_model_idle_for_period"] == [
        ("multi_model_idle_for_period", [MODEL], timedelta(minutes=15))
    ]
    assert client.units == ["target/0", "target/1", "target/2"]


def test_unit_rotation_rechecks_units_after_a_delayed_post_scale_failure() -> None:
    client = RecordingJujuClient(current_units=3)
    client.delay_scale_visibility_until_idle = True
    client.fail_next_scale_after_callback = True

    with pytest.raises(RuntimeError, match="scale extension failed"):
        _rotate_units(client, k8s_model=False)

    assert [call[1] for call in client.calls if call[0] == "remove_unit"] == ["target/3"]
    assert client.units == ["target/0", "target/1", "target/2"]
    assert client.application_units("target", MODEL) == client.units


def test_unit_rotation_continues_machine_cleanup_after_remove_hook_failure() -> None:
    client = RecordingJujuClient(current_units=3)
    client.fail_next_multi_model_idle = True
    client.fail_next_remove_after_callback = True
    client.extra_units_on_scale_up = 1

    with pytest.raises(TimeoutError, match="model failed to become idle") as error:
        _rotate_units(client, k8s_model=False)

    assert isinstance(error.value.__cause__, RuntimeError)
    assert str(error.value.__cause__) == "unit removal hook failed"
    assert [call[1] for call in client.calls if call[0] == "remove_unit"] == [
        "target/3",
        "target/surge-extra-0",
    ]
    assert len([call for call in client.calls if call[0] == "multi_model_idle_for_period"]) == 3
    assert client.units == ["target/0", "target/1", "target/2"]


def test_unit_rotation_waits_for_async_machine_removal_before_cleaning_surge() -> None:
    client = RecordingJujuClient(current_units=3)
    client.defer_next_remove = True
    client.fail_next_remove_after_callback = True

    with pytest.raises(RuntimeError, match="unit removal hook failed"):
        _rotate_units(client, k8s_model=False)

    assert [call[1] for call in client.calls if call[0] == "remove_unit"] == ["target/0"]
    assert client.units == ["target/1", "target/2", "target/3"]
    assert not client.pending_unit_removals
    assert len([call for call in client.calls if call[0] == "multi_model_idle_for_period"]) == 3


def test_unit_rotation_preserves_surge_when_async_removal_did_not_settle() -> None:
    client = RecordingJujuClient(current_units=3)
    client.defer_next_remove = True
    client.fail_next_remove_after_callback = True
    client.fail_multi_model_idle_on_calls = {2, 3}

    with pytest.raises(RuntimeError, match="unit removal hook failed") as error:
        _rotate_units(client, k8s_model=False)

    assert isinstance(error.value.__cause__, TimeoutError)
    assert client.units == ["target/0", "target/1", "target/2", "target/3"]
    assert client.pending_unit_removals == ["target/0"]
    assert [call[1] for call in client.calls if call[0] == "remove_unit"] == ["target/0"]

    # Once the deferred Juju removal completes, the retained surge keeps the application at capacity.
    client.fail_multi_model_idle_on_calls.clear()
    client.multi_model_idle_for_period([MODEL])
    assert client.units == ["target/1", "target/2", "target/3"]


def test_unit_rotation_preserves_machine_error_when_surge_cleanup_fails() -> None:
    client = RecordingJujuClient(current_units=3)
    client.fail_next_remove_after_callback = True
    client.fail_multi_model_idle_on_call = 2

    with pytest.raises(RuntimeError, match="unit removal hook failed") as error:
        _rotate_units(client, k8s_model=False)

    assert isinstance(error.value.__cause__, TimeoutError)
    assert str(error.value.__cause__) == "cleanup idle wait failed"
    assert client.units == ["target/1", "target/2", "target/3"]


def test_unit_rotation_continues_machine_cleanup_when_settling_wait_fails() -> None:
    client = RecordingJujuClient(current_units=3)
    client.fail_scale_after_callback_on_call = 1
    client.fail_multi_model_idle_on_call = 1

    with pytest.raises(RuntimeError, match="scale cleanup hook failed") as error:
        _rotate_units(client, k8s_model=False)

    assert isinstance(error.value.__cause__, TimeoutError)
    assert str(error.value.__cause__) == "cleanup idle wait failed"
    assert [call[1] for call in client.calls if call[0] == "remove_unit"] == ["target/3"]
    assert len([call for call in client.calls if call[0] == "multi_model_idle_for_period"]) == 2
    assert client.units == ["target/0", "target/1", "target/2"]


@pytest.mark.parametrize("statefulset", [True, False], ids=["statefulset", "deployment"])
def test_unit_rotation_replaces_kubernetes_pods_with_surge_capacity(statefulset: bool) -> None:
    client = RecordingJujuClient(current_units=2)
    kubernetes_client = RecordingKubernetesClient("target", 2, statefulset=statefulset)
    kubernetes_client.scale_down_uid = "scaled-2"
    kubernetes_client.defer_next_scale_down = True
    client.scale_callback = kubernetes_client.scale_to

    _rotate_units(client, k8s_model=True, kubernetes_client=kubernetes_client)

    assert [call for call in client.calls if call[0] == "scale_application"] == [
        ("scale_application", "target", 3, MODEL),
        ("scale_application", "target", 2, MODEL),
    ]
    assert [call[2] for call in kubernetes_client.calls if call[0] == "restart_pod"] == [
        "target-0" if statefulset else "target-hash-pod-1",
        "target-1" if statefulset else "target-hash-pod-2",
    ]
    assert [call for call in client.calls if call[0] == "multi_model_idle_for_period"] == [
        ("multi_model_idle_for_period", [MODEL], timedelta(minutes=15))
    ] * 4
    assert [call for call in client.calls if call[0] == "validate_model"] == [
        ("validate_model", MODEL, "simple"),
        ("validate_model", MODEL, "simple"),
        ("validate_model", MODEL, "deep"),
    ]
    assert client.calls[-1] == ("validate_model", MODEL, "deep")
    assert client.units == ["target/0", "target/1"]
    assert len(kubernetes_client.pods) == 2
    assert [call for call in kubernetes_client.calls if call[0] == "wait_for_charm_pods_ready"] == [
        ("wait_for_charm_pods_ready", "target", "model", 2, timedelta(minutes=15)),
        ("wait_for_charm_pods_ready", "target", "model", 3, timedelta(minutes=15)),
        ("wait_for_charm_pods_ready", "target", "model", 3, timedelta(minutes=15)),
        ("wait_for_charm_pods_ready", "target", "model", 3, timedelta(minutes=15)),
        ("wait_for_charm_pods_ready", "target", "model", 3, timedelta(minutes=15)),
        ("wait_for_charm_pods_ready", "target", "model", 2, timedelta(minutes=15)),
        ("wait_for_charm_pods_ready", "target", "model", 2, timedelta(minutes=15)),
    ]
    assert [call for call in kubernetes_client.calls if call[0] == "wait_for_charm_pods_replaced"] == [
        (
            "wait_for_charm_pods_replaced",
            "target",
            "model",
            3,
            {"uid-0", "uid-1"},
            timedelta(minutes=15),
        ),
        (
            "wait_for_charm_pods_replaced",
            "target",
            "model",
            2,
            {"uid-0", "uid-1"},
            timedelta(minutes=15),
        ),
    ]
    assert not {f"uid-{index}" for index in range(2)} & {
        pod.metadata.uid for pod in kubernetes_client.pods if pod.metadata is not None
    }


def test_kubernetes_rotation_dispatches_restart_hook_once_per_unit() -> None:
    class RestartRecorder(JujuExtension):
        def __init__(self) -> None:
            self.restarted_units: list[tuple[JujuModelHandle, str]] = []

        def post_restart_unit(self, model: JujuModelHandle, unit: str) -> None:
            self.restarted_units.append((model, unit))

    client = RecordingJujuClient(current_units=2)
    kubernetes_client = RecordingKubernetesClient("target", 2)
    client.scale_callback = kubernetes_client.scale_to
    backend = RecordingJujuBackend(
        k8s_model=True,
        kubernetes_client=cast(KubernetesClient, kubernetes_client),
    )
    extension = RestartRecorder()
    juju_client = RecordingRotationJujuClient(client, backend, extensions=[extension])

    juju_client.rotate_application_units("target", MODEL)

    assert extension.restarted_units == [(MODEL, "target/0"), (MODEL, "target/1")]


def test_unit_rotation_snapshots_ready_pods_after_terminating_pod_converges() -> None:
    client = RecordingJujuClient(current_units=2)
    kubernetes_client = RecordingKubernetesClient("target", 2, statefulset=False)
    terminating_pod = kubernetes_client.pods[0]
    assert terminating_pod.metadata is not None
    assert terminating_pod.metadata.uid is not None
    terminating_pod.metadata.deletion_timestamp = datetime(2026, 10, 14, tzinfo=timezone.utc)
    kubernetes_client.terminating_pod_uids.add(terminating_pod.metadata.uid)
    kubernetes_client.pods.append(kubernetes_client._pod(0, "replacement-0"))
    client.scale_callback = kubernetes_client.scale_to

    _rotate_units(client, k8s_model=True, kubernetes_client=kubernetes_client)

    first_restart = next(call for call in kubernetes_client.calls if call[0] == "restart_pod")
    assert first_restart[3] == "replacement-0"
    assert len(kubernetes_client.pods) == 2


def test_unit_rotation_preserves_surge_after_kubernetes_replacement_failure() -> None:
    client = RecordingJujuClient(current_units=2)
    kubernetes_client = RecordingKubernetesClient("target", 2, statefulset=False)
    kubernetes_client.fail_restart_pod = True
    client.scale_callback = kubernetes_client.scale_to

    with pytest.raises(TimeoutError, match="replacement pod did not appear"):
        _rotate_units(client, k8s_model=True, kubernetes_client=kubernetes_client)

    assert [call for call in client.calls if call[0] == "scale_application"] == [
        ("scale_application", "target", 3, MODEL),
    ]
    assert client.units == ["target/0", "target/1", "target/2"]
    assert len(kubernetes_client.pods) == 3
    assert [call[3] for call in kubernetes_client.calls if call[0] == "wait_for_charm_pods_ready"] == [2, 3]
    assert [call for call in kubernetes_client.calls if call[0] == "wait_for_charm_pods_replaced"]


def test_unit_rotation_waits_for_kubernetes_surge_readiness_before_deleting_originals() -> None:
    client = RecordingJujuClient(current_units=2)
    kubernetes_client = RecordingKubernetesClient("target", 2, statefulset=False)
    kubernetes_client.unready_pod_uids.add("scaled-2")
    client.scale_callback = kubernetes_client.scale_to

    with pytest.raises(TimeoutError, match="a charm pod is not ready"):
        _rotate_units(client, k8s_model=True, kubernetes_client=kubernetes_client)

    assert [call for call in client.calls if call[0] == "scale_application"] == [
        ("scale_application", "target", 3, MODEL),
    ]
    assert not [call for call in kubernetes_client.calls if call[0] == "restart_pod"]
    assert [call[3] for call in kubernetes_client.calls if call[0] == "wait_for_charm_pods_ready"] == [2, 3]
    assert client.units == ["target/0", "target/1", "target/2"]
    assert len(kubernetes_client.pods) == 3


def test_unit_rotation_preserves_kubernetes_error_when_cleanup_fails() -> None:
    client = RecordingJujuClient(current_units=2)
    kubernetes_client = RecordingKubernetesClient("target", 2, statefulset=False)
    kubernetes_client.fail_restart_pod = True
    client.fail_multi_model_idle_on_call = 2
    client.scale_callback = kubernetes_client.scale_to

    with pytest.raises(TimeoutError, match="replacement pod did not appear") as error:
        _rotate_units(client, k8s_model=True, kubernetes_client=kubernetes_client)

    assert isinstance(error.value.__cause__, TimeoutError)
    assert str(error.value.__cause__) == "cleanup idle wait failed"
    assert [call[3] for call in kubernetes_client.calls if call[0] == "wait_for_charm_pods_ready"] == [2, 3]


def test_unit_rotation_preserves_surge_after_kubernetes_readiness_failure() -> None:
    client = RecordingJujuClient(current_units=2)
    kubernetes_client = RecordingKubernetesClient("target", 2, statefulset=False)
    kubernetes_client.unready_pod_uids.add("replacement-0")
    client.scale_callback = kubernetes_client.scale_to

    with pytest.raises(TimeoutError, match="a charm pod is not ready"):
        _rotate_units(client, k8s_model=True, kubernetes_client=kubernetes_client)

    assert [call for call in client.calls if call[0] == "scale_application"] == [
        ("scale_application", "target", 3, MODEL),
    ]
    assert client.units == ["target/0", "target/1", "target/2"]
    assert len(kubernetes_client.pods) == 3
    assert [call[3] for call in kubernetes_client.calls if call[0] == "wait_for_charm_pods_ready"] == [2, 3, 3]


def test_unit_rotation_preserves_surge_when_post_scale_hook_fails() -> None:
    client = RecordingJujuClient(current_units=2)
    client.fail_next_scale_after_callback = True
    kubernetes_client = RecordingKubernetesClient("target", 2, statefulset=False)
    client.scale_callback = kubernetes_client.scale_to

    with pytest.raises(RuntimeError, match="scale extension failed"):
        _rotate_units(client, k8s_model=True, kubernetes_client=kubernetes_client)

    assert [call for call in client.calls if call[0] == "scale_application"] == [
        ("scale_application", "target", 3, MODEL),
    ]
    assert client.units == ["target/0", "target/1", "target/2"]
    assert len(kubernetes_client.pods) == 3
    assert [call[3] for call in kubernetes_client.calls if call[0] == "wait_for_charm_pods_ready"] == [2]


def test_unit_rotation_does_not_scale_down_when_deployment_could_remove_unrotated_pod() -> None:
    client = RecordingJujuClient(current_units=2)
    kubernetes_client = RecordingKubernetesClient("target", 2, statefulset=False)
    kubernetes_client.fail_restart_after_delete = True
    kubernetes_client.scale_down_uid = "uid-1"
    client.scale_callback = kubernetes_client.scale_to

    with pytest.raises(TimeoutError, match="replacement pod did not appear"):
        _rotate_units(client, k8s_model=True, kubernetes_client=kubernetes_client)

    assert [call for call in client.calls if call[0] == "scale_application"] == [
        ("scale_application", "target", 3, MODEL),
    ]
    assert client.units == ["target/0", "target/1", "target/2"]
    remaining_uids = {pod.metadata.uid for pod in kubernetes_client.pods if pod.metadata is not None}
    assert "uid-1" in remaining_uids
    assert kubernetes_client.scale_down_uid == "uid-1"


def test_unit_rotation_detects_deployment_scale_down_removing_a_mapped_unit() -> None:
    client = RecordingJujuClient(current_units=2)
    kubernetes_client = RecordingKubernetesClient("target", 2, statefulset=False)
    kubernetes_client.scale_down_uid = "replacement-0"
    kubernetes_client.defer_next_scale_down = True
    client.scale_callback = kubernetes_client.scale_to

    with pytest.raises(RuntimeError, match="Unable to uniquely map Kubernetes pod"):
        _rotate_units(client, k8s_model=True, kubernetes_client=kubernetes_client)

    assert [call for call in client.calls if call[0] == "scale_application"] == [
        ("scale_application", "target", 3, MODEL),
        ("scale_application", "target", 2, MODEL),
    ]
    assert client.units == ["target/0", "target/1"]
    pod_units = {
        (pod.metadata.annotations or {})["unit.juju.is/id"]
        for pod in kubernetes_client.pods
        if pod.metadata is not None
    }
    assert pod_units == {"target/1", "target/2"}


def test_unit_rotation_preserves_surge_when_failure_cleanup_cannot_map_units() -> None:
    client = RecordingJujuClient(current_units=2)
    client.fail_multi_model_idle_on_call = 3
    kubernetes_client = RecordingKubernetesClient("target", 2, statefulset=False)
    kubernetes_client.invalid_unit_mapping_uid = "replacement-1"
    client.scale_callback = kubernetes_client.scale_to

    with pytest.raises(TimeoutError, match="cleanup idle wait failed") as error:
        _rotate_units(client, k8s_model=True, kubernetes_client=kubernetes_client)

    assert isinstance(error.value.__cause__, RuntimeError)
    assert "Unable to uniquely map" in str(error.value.__cause__)
    assert [call for call in client.calls if call[0] == "scale_application"] == [
        ("scale_application", "target", 3, MODEL),
    ]
    assert client.units == ["target/0", "target/1", "target/2"]
    assert len(kubernetes_client.pods) == 3


def test_unit_rotation_waits_for_kubernetes_recovery_when_cleanup_idle_wait_fails() -> None:
    client = RecordingJujuClient(current_units=2)
    client.fail_multi_model_idle_on_call = 4
    kubernetes_client = RecordingKubernetesClient("target", 2, statefulset=False)
    client.scale_callback = kubernetes_client.scale_to

    with pytest.raises(TimeoutError, match="cleanup idle wait failed"):
        _rotate_units(client, k8s_model=True, kubernetes_client=kubernetes_client)

    assert client.units == ["target/0", "target/1"]
    assert len(kubernetes_client.pods) == 2
    assert [call[3] for call in kubernetes_client.calls if call[0] == "wait_for_charm_pods_ready"] == [
        2,
        3,
        3,
        3,
        3,
        2,
        2,
    ]


def test_unit_rotation_waits_for_kubernetes_recovery_when_scale_down_hook_fails() -> None:
    client = RecordingJujuClient(current_units=2)
    client.fail_scale_after_callback_on_call = 2
    kubernetes_client = RecordingKubernetesClient("target", 2, statefulset=False)
    client.scale_callback = kubernetes_client.scale_to

    with pytest.raises(RuntimeError, match="scale cleanup hook failed"):
        _rotate_units(client, k8s_model=True, kubernetes_client=kubernetes_client)

    assert client.units == ["target/0", "target/1"]
    assert len(kubernetes_client.pods) == 2
    assert [call[3] for call in kubernetes_client.calls if call[0] == "wait_for_charm_pods_ready"] == [
        2,
        3,
        3,
        3,
        3,
        2,
        2,
    ]
